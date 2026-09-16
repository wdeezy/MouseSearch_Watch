import unittest

from app import build_mam_search_params, search_opt_checkbox


class SearchParamBuilderTests(unittest.TestCase):
    def test_defaults_when_no_field_checkboxes_present(self):
        params = build_mam_search_params({"query": ["dune"]})
        self.assertEqual(params["tor[text]"], "dune")
        self.assertEqual(params["tor[sortType]"], "default")
        self.assertEqual(params["tor[searchType]"], "all")
        self.assertEqual(params["tor[browse_lang][]"], ["1"])
        self.assertEqual(params["tor[srchIn][title]"], "true")
        self.assertEqual(params["tor[srchIn][author]"], "true")
        self.assertEqual(params["tor[srchIn][series]"], "true")
        self.assertNotIn("tor[srchIn][narrator]", params)
        self.assertNotIn("tor[main_cat][]", params)

    def test_explicit_field_selection_disables_unlisted_fields(self):
        params = build_mam_search_params({"query": "dune", "search_in_title": "true"})
        self.assertEqual(params["tor[srchIn][title]"], "true")
        self.assertNotIn("tor[srchIn][author]", params)
        self.assertNotIn("tor[srchIn][series]", params)

    def test_boolean_values_are_accepted_for_watch_definitions(self):
        self.assertTrue(search_opt_checkbox({"search_in_title": True}, "search_in_title"))
        self.assertFalse(search_opt_checkbox({"search_in_title": True}, "search_in_author"))

    def test_author_search_forces_title_search(self):
        params = build_mam_search_params({"search_in_author": "true"})
        self.assertEqual(params["tor[srchIn][title]"], "true")
        self.assertEqual(params["tor[srchIn][author]"], "true")

    def test_categories_flags_dates_and_stats(self):
        params = build_mam_search_params({
            "query": "The Economist",
            "search_in_title": True,
            "language_ids": ["1", "2"],
            "main_cat": ["14"],
            "category_ids": ["61"],
            "flag_ids": ["2"],
            "flags_mode": "1",
            "start_date": "2026-01-01",
            "min_seeders": "1",
            "sort_type": "dateDesc",
        }, perpage=25)
        self.assertEqual(params["tor[browse_lang][]"], ["1", "2"])
        self.assertEqual(params["tor[main_cat][]"], ["14"])
        self.assertEqual(params["tor[cat][]"], ["61"])
        self.assertEqual(params["tor[browseFlags][]"], ["2"])
        self.assertEqual(params["tor[browseFlagsHideVsShow]"], "1")
        self.assertEqual(params["tor[startDate]"], "2026-01-01")
        self.assertEqual(params["tor[minSeeders]"], "1")
        self.assertEqual(params["tor[sortType]"], "dateDesc")
        self.assertEqual(params["perpage"], 25)

    def test_all_main_cat_means_no_filter(self):
        params = build_mam_search_params({"main_cat": ["all", "14"]})
        self.assertNotIn("tor[main_cat][]", params)

    def test_language_name_fallback(self):
        params = build_mam_search_params({"language": "English"})
        self.assertEqual(params["tor[browse_lang][]"], ["1"])
