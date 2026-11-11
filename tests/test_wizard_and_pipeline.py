import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.config import load_scoring, load_settings, load_templates, validate_targeting
from modules.demo import demo_enricher, demo_index, demo_itunes, demo_searcher
from modules.pipeline import discover
from modules.prompts import BACK, ScriptedPrompter
from modules.scraper import ApifyClient, estimate_cost, keyword_plan, search_jobs
from modules.wizard import run_wizard


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings()
        self.templates = load_templates(self.settings)
        self.summary = {"status": "Connected", "existing_leads": 3}
        self.base = validate_targeting(
            {
                "confirmed": False,
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity", "Health & Fitness"],
                "custom_keywords": [],
                "max_ratings": 30,
                "max_app_age": "2y",
                "last_update": "90d",
                "min_score": 50,
                "target_prospects": 30,
                "country": "us",
                "template_id": "intro",
            }
        )

    def test_questions_use_defaults_and_back_returns_to_the_previous_answer(self):
        prompter = ScriptedPrompter(
            [
                "solo",
                BACK,
                "startup",
                "ios",
                "continue",
                ["Health & Fitness"],
                "water reminder",
                10,
                "6m",
                "30d",
                70,
                20,
                "start",
            ]
        )
        shown = []
        original = prompter.select

        def select(message, choices, default=None):
            shown.append((message, list(choices)))
            return original(message, choices, default)

        prompter.select = select
        result = run_wizard(prompter, self.base, self.settings, self.templates, self.summary)
        self.assertEqual(prompter.prompts[0], "Which ICP are you targeting?")
        self.assertIn(("Solo developer", "solo"), shown[0][1])
        self.assertIn(("Startups", "startup"), shown[0][1])
        self.assertIn(("iOS / Apple App Store", "ios"), shown[1][1])
        self.assertEqual(result["developer_type"], "startup")
        self.assertEqual(result["categories"], ["Health & Fitness"])
        self.assertEqual(result["custom_keywords"], ["water reminder"])
        self.assertEqual(result["max_ratings"], 10)
        self.assertEqual(result["target_prospects"], 20)
        self.assertTrue(result["confirmed"])

    def test_old_audience_value_loads_as_an_icp(self):
        loaded = validate_targeting({"developer_type": "solo_small", "categories": ["Productivity"]})
        self.assertEqual(loaded["developer_type"], "solo")
        open_cap = validate_targeting(
            {"developer_type": "scaleup", "max_ratings": "any", "categories": ["Productivity"]}
        )
        self.assertEqual(open_cap["max_ratings"], "any")

    def test_saved_configuration_can_be_edited_without_repeating_every_question(self):
        saved = dict(self.base)
        saved["confirmed"] = True
        prompter = ScriptedPrompter(["continue", "edit", "ratings", 10, "start"])
        result = run_wizard(prompter, saved, self.settings, self.templates, self.summary)
        self.assertEqual(result["max_ratings"], 10)
        self.assertEqual(result["platforms"], "ios")
        self.assertEqual(result["min_score"], 50)

    def test_cancel_on_the_summary_does_not_return_a_configuration(self):
        saved = dict(self.base)
        saved["confirmed"] = True
        result = run_wizard(ScriptedPrompter(["continue", "cancel"]), saved, self.settings, self.templates, self.summary)
        self.assertIsNone(result)


class PipelineTests(unittest.TestCase):
    def test_keyword_plan_round_robins_categories(self):
        settings = load_settings()
        targeting = validate_targeting(
            {
                "categories": ["Productivity", "Health & Fitness"],
                "custom_keywords": ["custom tracker"],
                "platforms": "both",
            }
        )
        planned = keyword_plan(targeting, settings)
        self.assertEqual(planned[0], "focus timer")
        self.assertEqual(planned[1], "habit tracker")
        self.assertIn("custom tracker", planned)
        jobs = search_jobs(targeting, settings)
        self.assertLessEqual(len(jobs), settings["apify"]["max_searches_per_run"])
        self.assertGreater(estimate_cost(jobs, settings), 0)

    def test_store_language_matches_each_actor(self):
        settings = load_settings()
        client = ApifyClient("test-token", settings)
        self.assertEqual(client._input("ios", "habit tracker", "us")["lang"], "en-us")
        self.assertEqual(client._input("android", "habit tracker", "us")["lang"], "en")

    def test_demo_discovery_partitions_and_qualifies_the_sample(self):
        settings = load_settings()
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity", "Health & Fitness"],
                "max_ratings": 30,
                "max_app_age": "2y",
                "last_update": "90d",
                "min_score": 50,
                "target_prospects": 30,
                "country": "us",
            }
        )
        discovery = discover(
            targeting,
            settings,
            scoring,
            demo_index(),
            searcher=demo_searcher(),
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
        )
        stats = discovery.stats
        accounted = (
            stats["already_in_crm"]
            + stats["filtered_out"]
            + stats["missing_developer_site"]
            + stats["missing_public_email"]
            + stats["qualified"]
        )
        self.assertEqual(accounted, stats["discovered"])
        names = {lead.app_name for lead in discovery.qualified}
        self.assertEqual(names, {"Quiet Focus"})
        quiet = next(lead for lead in discovery.qualified if lead.app_name == "Quiet Focus")
        self.assertEqual(quiet.developer_type, "solo")
        self.assertEqual(quiet.score_normalized, 100)
        self.assertEqual(quiet.primary_email.address, "jane@quiet.example")
        self.assertEqual(quiet.primary_email.email_type, "founder")
        self.assertEqual(quiet.contact_name, "Jane Doe")
        self.assertEqual({lead.app_name for lead in discovery.missing_site}, {"Paper Day"})
        self.assertEqual({lead.app_name for lead in discovery.missing_email}, {"Pocket Budget"})
        self.assertEqual(stats["already_in_crm"], 1)
        self.assertGreaterEqual(stats["filtered_out"], 4)
        self.assertFalse(stats["errors"])


if __name__ == "__main__":
    unittest.main()
