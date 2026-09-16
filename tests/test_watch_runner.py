import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watches import runner, store


def _result(mam_id, title, *, added="2026-09-10 12:00:00", my_snatched=0):
    return {
        "id": mam_id,
        "title": title,
        "added": added,
        "size": "50 MiB",
        "seeders": 10,
        "my_snatched": my_snatched,
        "download_link": f"https://mam.example/download/{mam_id}",
        "main_cat": "14",
    }


class FakeDeps:
    def __init__(self, results, add_result=None):
        self.results = results
        self.add_result = add_result or {"message": "Torrent added", "hash": "abc"}
        self.params = []
        self.added = []
        self.notifications = []
        self.toasts = []
        self.login_ok = True

    async def login(self):
        return self.login_ok

    def build_params(self, opts, *, perpage=None):
        self.params.append(opts)
        return {"opts": opts}

    async def search(self, params):
        if isinstance(self.results, Exception):
            raise self.results
        return list(self.results)

    async def add(self, item, **kwargs):
        self.added.append((item["id"], kwargs))
        result = self.add_result
        if callable(result):
            return result(item)
        return dict(result)

    async def notify(self, event, success, **details):
        self.notifications.append((event, success, details))

    async def toast(self, message, category="primary"):
        self.toasts.append((message, category))


class WatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store.configure(Path(self.tmp.name) / "watches.db")
        self.watch = store.upsert_watch({
            "name": "Economist",
            "query": "The Economist US Edition",
            "title_regex": r"^The Economist US Edition",
            "max_grabs_per_run": 3,
            "torrent_category": "watch-test",
            "interval_minutes": 60,
        })
        self.results = [
            _result("1", "The Economist US Edition - September 12 2026", added="2026-09-12 08:00:00"),
            _result("2", "The Economist UK Edition - September 12 2026", added="2026-09-12 08:00:00"),
            _result("3", "The Economist US Edition - September 5 2026", added="2026-09-05 08:00:00"),
            _result("4", "The Economist US Edition - August 29 2026", added="2026-08-29 08:00:00", my_snatched=1),
        ]

    def tearDown(self):
        store.close()
        runner.configure(
            build_params=None, search=None, add=None, login=None,
            notify=runner._noop_async, toast=runner._noop_async, config=runner._default_config,
        )
        self.tmp.cleanup()

    def _wire(self, fake):
        runner.configure(
            build_params=fake.build_params, search=fake.search, add=fake.add, login=fake.login,
            notify=fake.notify, toast=fake.toast,
        )
        return fake

    def _run(self, *, dry_run, watch=None):
        return asyncio.run(runner.run_watch(watch or store.get_watch(self.watch["id"]), dry_run=dry_run))

    def test_regex_filter_dedupe_and_grab(self):
        fake = self._wire(FakeDeps(self.results))
        summary = self._run(dry_run=False)

        self.assertEqual(summary["checked"], 4)
        self.assertEqual(summary["matched"], 3)
        self.assertEqual(summary["grabbed"], 2)
        self.assertEqual(summary["skipped_seen"], 1)
        self.assertEqual(summary["errors"], 0)
        # newest first
        self.assertEqual([mid for mid, _ in fake.added], ["1", "3"])
        self.assertEqual(fake.added[0][1]["category"], "watch-test")
        actions = {item["id"]: item["action"] for item in summary["items"]}
        self.assertEqual(actions, {"1": "grabbed", "2": "no_match", "3": "grabbed", "4": "skipped_seen"})

        self.assertEqual(store.seen_ids(self.watch["id"]), {"1", "3", "4"})
        kinds = [e["kind"] for e in store.events_for(self.watch["id"], 50)]
        self.assertEqual(kinds.count("grabbed"), 2)
        self.assertEqual(kinds.count("matched"), 2)
        self.assertIn("checked", kinds)
        self.assertEqual(len(fake.notifications), 2)
        self.assertEqual(fake.notifications[0][0], "watch_grabbed")
        self.assertEqual(len(fake.toasts), 2)

        refreshed = store.get_watch(self.watch["id"])
        self.assertTrue(refreshed["last_run_at"])
        self.assertEqual(refreshed["last_error"], "")
        self.assertIn("grabbed 2", refreshed["last_summary"])

        # Second run: everything already seen, nothing added.
        fake.added.clear()
        summary2 = self._run(dry_run=False)
        self.assertEqual(summary2["grabbed"], 0)
        self.assertEqual(summary2["skipped_seen"], 3)
        self.assertEqual(fake.added, [])
        # last_run_at drives a start_date lookback in the next search params.
        self.assertIn("start_date", fake.params[-1])
        self.assertEqual(fake.params[-1]["sort_type"], "dateDesc")

    def test_dry_run_grabs_nothing_and_writes_nothing(self):
        fake = self._wire(FakeDeps(self.results))
        summary = self._run(dry_run=True)
        self.assertEqual(summary["would_grab"], 2)
        self.assertEqual(summary["grabbed"], 0)
        self.assertEqual(fake.added, [])
        actions = {item["id"]: item["action"] for item in summary["items"]}
        self.assertEqual(actions["1"], "would_grab")
        self.assertEqual(actions["4"], "skipped_seen")
        self.assertEqual(store.seen_ids(self.watch["id"]), set())
        self.assertEqual(store.events_for(self.watch["id"]), [])
        self.assertIsNone(store.get_watch(self.watch["id"])["last_run_at"])

    def test_unsaved_watch_can_be_previewed(self):
        fake = self._wire(FakeDeps(self.results))
        unsaved = store.normalize_watch({"name": "x", "query": "Economist", "title_regex": "US Edition"})
        summary = asyncio.run(runner.run_watch(unsaved, dry_run=True))
        self.assertEqual(summary["would_grab"], 2)
        self.assertEqual(fake.added, [])

    def test_cap_limits_grabs_and_logs_overflow(self):
        store.upsert_watch({"id": self.watch["id"], "max_grabs_per_run": 1})
        fake = self._wire(FakeDeps(self.results))
        summary = self._run(dry_run=False)
        self.assertEqual(summary["grabbed"], 1)
        self.assertEqual(summary["skipped_cap"], 1)
        self.assertEqual([mid for mid, _ in fake.added], ["1"])
        self.assertEqual(store.seen_ids(self.watch["id"]), {"1", "4"})
        kinds = [e["kind"] for e in store.events_for(self.watch["id"], 50)]
        self.assertIn("skipped_cap", kinds)
        # The capped one is grabbed on the next run.
        summary2 = self._run(dry_run=False)
        self.assertEqual(summary2["grabbed"], 1)
        self.assertEqual([mid for mid, _ in fake.added], ["1", "3"])

    def test_add_errors_do_not_mark_seen(self):
        fake = self._wire(FakeDeps(self.results, add_result={"error": "client unreachable"}))
        summary = self._run(dry_run=False)
        self.assertEqual(summary["grabbed"], 0)
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(store.seen_ids(self.watch["id"]), {"4"})
        refreshed = store.get_watch(self.watch["id"])
        self.assertEqual(refreshed["last_error"], "client unreachable")
        kinds = [e["kind"] for e in store.events_for(self.watch["id"], 50)]
        self.assertEqual(kinds.count("error"), 2)
        self.assertEqual(fake.notifications, [])

        # Client back: retried and grabbed.
        fake.add_result = {"message": "ok", "hash": "h"}
        summary2 = self._run(dry_run=False)
        self.assertEqual(summary2["grabbed"], 2)
        self.assertEqual(store.seen_ids(self.watch["id"]), {"1", "3", "4"})

    def test_insufficient_buffer_is_an_error_and_retries(self):
        fake = self._wire(FakeDeps(self.results, add_result={"status": "insufficient_buffer", "message": "Insufficient buffer"}))
        summary = self._run(dry_run=False)
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(store.seen_ids(self.watch["id"]), {"4"})

    def test_search_failure_is_recorded(self):
        fake = self._wire(FakeDeps(RuntimeError("MAM 503")))
        summary = self._run(dry_run=False)
        self.assertEqual(summary["error"], "MAM 503")
        self.assertEqual(summary["checked"], 0)
        refreshed = store.get_watch(self.watch["id"])
        self.assertEqual(refreshed["last_error"], "MAM 503")
        self.assertTrue(refreshed["last_run_at"])
        self.assertEqual(store.events_for(self.watch["id"])[0]["kind"], "error")

    def test_login_failure_is_recorded(self):
        fake = self._wire(FakeDeps(self.results))
        fake.login_ok = False
        summary = self._run(dry_run=True)
        self.assertIn("login failed", summary["error"].lower())
        self.assertEqual(fake.added, [])

    def test_watch_to_search_opts(self):
        watch = dict(self.watch, search_fields=["title", "author"], min_seeders=2,
                     last_run_at="2026-09-16T10:00:00+00:00", main_cat=["14"], category_ids=["61"])
        opts = runner.watch_to_search_opts(watch)
        self.assertTrue(opts["search_in_title"])
        self.assertTrue(opts["search_in_author"])
        self.assertFalse(opts["search_in_series"])
        self.assertEqual(opts["min_seeders"], "2")
        self.assertEqual(opts["start_date"], "2026-09-15")
        self.assertEqual(opts["main_cat"], ["14"])
        self.assertEqual(opts["sort_type"], "dateDesc")

    def test_is_due(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        base = dict(self.watch, enabled=True, interval_minutes=60)
        self.assertTrue(runner.is_due(dict(base, last_run_at=None), now))
        self.assertFalse(runner.is_due(dict(base, last_run_at=(now - timedelta(minutes=30)).isoformat()), now))
        self.assertTrue(runner.is_due(dict(base, last_run_at=(now - timedelta(minutes=61)).isoformat()), now))
        self.assertFalse(runner.is_due(dict(base, enabled=False, last_run_at=None), now))

    def test_run_all_watches_isolates_failures(self):
        other = store.upsert_watch({"name": "Broken", "query": "x", "title_regex": "x", "interval_minutes": 60})
        calls = []

        async def flaky_run(watch, *, dry_run):
            calls.append(watch["name"])
            if watch["name"] == "Broken":
                raise RuntimeError("kaboom")
            return {"summary_text": "ok"}

        original = runner.run_watch
        runner.run_watch = flaky_run
        try:
            summaries = asyncio.run(runner.run_all_watches(pause_seconds=0))
        finally:
            runner.run_watch = original
        self.assertEqual(sorted(calls), ["Broken", "Economist"])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(store.get_watch(other["id"])["last_error"], "kaboom")
