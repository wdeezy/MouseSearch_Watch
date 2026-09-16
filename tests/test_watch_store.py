import tempfile
import unittest
from pathlib import Path

from watches import store


class WatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.configure(Path(self.tmp.name) / "watches.db")

    def tearDown(self):
        store.close()
        self.tmp.cleanup()

    def _make(self, **overrides):
        data = {
            "name": "Economist",
            "query": "The Economist US Edition",
            "title_regex": r"^The Economist US Edition",
            "language_ids": ["1"],
            "main_cat": ["14"],
            "category_ids": ["61"],
            "interval_minutes": 60,
            "torrent_category": "watch-test",
        }
        data.update(overrides)
        return store.upsert_watch(data)

    def test_create_and_list_round_trip(self):
        watch = self._make()
        self.assertIsInstance(watch["id"], int)
        self.assertTrue(watch["enabled"])
        self.assertEqual(watch["main_cat"], ["14"])
        self.assertEqual(watch["category_ids"], ["61"])
        self.assertEqual(watch["search_fields"], ["title"])
        self.assertEqual(watch["max_grabs_per_run"], 3)
        self.assertEqual([w["name"] for w in store.list_watches()], ["Economist"])
        self.assertEqual(store.get_watch(watch["id"])["query"], "The Economist US Edition")

    def test_update_keeps_unspecified_fields(self):
        watch = self._make()
        updated = store.upsert_watch({"id": watch["id"], "enabled": False, "interval_minutes": 120})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["interval_minutes"], 120)
        self.assertEqual(updated["title_regex"], r"^The Economist US Edition")
        self.assertEqual(len(store.list_watches()), 1)

    def test_update_unknown_id_is_rejected(self):
        with self.assertRaises(store.WatchValidationError):
            store.upsert_watch({"id": 999, "name": "x", "query": "y"})

    def test_bad_regex_is_rejected(self):
        with self.assertRaises(store.WatchValidationError) as ctx:
            self._make(title_regex="^The (Economist")
        self.assertIn("title_regex", str(ctx.exception))

    def test_name_and_query_required(self):
        with self.assertRaises(store.WatchValidationError):
            self._make(name="  ")
        with self.assertRaises(store.WatchValidationError):
            self._make(query="", title_regex="")

    def test_interval_minimum_and_cap_minimum(self):
        with self.assertRaises(store.WatchValidationError):
            self._make(interval_minutes=1)
        with self.assertRaises(store.WatchValidationError):
            self._make(max_grabs_per_run=0)

    def test_list_fields_accept_csv_and_json_strings(self):
        watch = self._make(language_ids="1,2", category_ids='["61", "62"]', main_cat=["all", "14"])
        self.assertEqual(watch["language_ids"], ["1", "2"])
        self.assertEqual(watch["category_ids"], ["61", "62"])
        self.assertEqual(watch["main_cat"], ["14"])

    def test_seen_table(self):
        watch = self._make()
        self.assertFalse(store.is_seen(watch["id"], "123"))
        store.mark_seen(watch["id"], 123, False, title="Issue 1")
        self.assertTrue(store.is_seen(watch["id"], "123"))
        store.mark_seen(watch["id"], "123", True)
        seen = store.list_seen(watch["id"])
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["grabbed"])
        self.assertEqual(seen[0]["title"], "Issue 1")
        self.assertEqual(store.seen_ids(watch["id"]), {"123"})
        self.assertEqual(store.mark_many_seen(watch["id"], [{"id": "5"}, "6", {"id": ""}]), 2)
        self.assertEqual(store.clear_seen(watch["id"], "5"), 1)
        self.assertEqual(store.clear_seen(watch["id"]), 2)
        self.assertEqual(store.seen_ids(watch["id"]), set())

    def test_events_and_prune(self):
        watch = self._make()
        for i in range(5):
            store.log_event(watch["id"], "matched", mam_id=str(i), title=f"t{i}")
        store.log_event(watch["id"], "grabbed", mam_id="4", title="t4", detail="ok")
        events = store.events_for(watch["id"], limit=3)
        self.assertEqual([e["kind"] for e in events], ["grabbed", "matched", "matched"])
        self.assertEqual(events[0]["mam_id"], "4")
        with self.assertRaises(ValueError):
            store.log_event(watch["id"], "bogus")
        self.assertEqual(store.prune_events(watch["id"], keep=2), 4)
        self.assertEqual(len(store.events_for(watch["id"], limit=50)), 2)

    def test_delete_cascades(self):
        watch = self._make()
        store.mark_seen(watch["id"], "1", True)
        store.log_event(watch["id"], "checked", detail="x")
        self.assertTrue(store.delete_watch(watch["id"]))
        self.assertFalse(store.delete_watch(watch["id"]))
        self.assertIsNone(store.get_watch(watch["id"]))
        self.assertEqual(store.events_for(watch["id"]), [])
        self.assertEqual(store.seen_ids(watch["id"]), set())

    def test_run_state(self):
        watch = self._make()
        store.update_run_state(watch["id"], last_run_at="2026-09-16T10:00:00+00:00", last_error="boom", last_summary="s")
        refreshed = store.get_watch(watch["id"])
        self.assertEqual(refreshed["last_run_at"], "2026-09-16T10:00:00+00:00")
        self.assertEqual(refreshed["last_error"], "boom")
        self.assertEqual(refreshed["last_summary"], "s")
