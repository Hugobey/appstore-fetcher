import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.config import load_scoring, load_settings, load_templates, validate_targeting
from modules.dates import utc_today
from contextlib import nullcontext

from modules.activity import Narrator
from modules.agent_ui import AgentDashboard
from modules.deduplication import DedupIndex
from modules.demo import demo_enricher, demo_index, demo_itunes, demo_searcher, sample_items
from modules.models import Lead
from modules.pipeline import discover
from modules.prompts import BACK, ScriptedPrompter
from modules.category_feed import category_plan, category_searcher, chart_url, entry_ids, fetch_genre_apps, lookup_url
from modules.preview import format_summary
from modules.scraper import ApifyClient, search_jobs
from modules.scoring import classify_developer, hard_filter_reason
from modules.enricher import filter_by_mrr_ranges, hunt_target
from modules.labels import ONBOARDING_CATEGORIES
from modules.wizard import _ask_category, _ask_country, _ask_mrr_ranges, _ask_ratings, _steps, run_wizard


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
                "indie_founder",
                "freemium",
                BACK,
                "freemium",
                ["Health & Fitness"],
                [],
                ["solo"],
                "any",
                250,
                "2y",
                "180d",
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
        self.assertEqual(prompter.prompts[0], "👤 What best describes you?")
        self.assertIn(("Angel Investor / VC", "angel_vc"), shown[0][1])
        self.assertIn(("Growth Consultant / Agency", "growth_agency"), shown[0][1])
        self.assertIn(("Indie App Founder / Other", "indie_founder"), shown[0][1])
        self.assertEqual(len(shown[0][1]), 3)
        self.assertEqual(prompter.prompts[1], "💳 App Pricing Model")
        self.assertIn("💰 MRR Ranges", prompter.prompts)
        self.assertNotIn("💰 Consider Estimated Revenue / MRR?", prompter.prompts)
        self.assertFalse(any("email template" in prompt.lower() for prompt in prompter.prompts))
        self.assertFalse(any(prompt.startswith("⚡") for prompt in prompter.prompts))
        self.assertNotIn("💰 Max MRR to include:", prompter.prompts)
        self.assertIn("🎯 Which ICP are you targeting?", prompter.prompts)
        self.assertIn("⭐ What is the minimum number of ratings an app can have?", prompter.prompts)
        self.assertIn("⭐ What is the maximum number of ratings an app can have?", prompter.prompts)
        self.assertIn("📅 How old can the app be?", prompter.prompts)
        self.assertIn("🔄 How recently must the app have been updated?", prompter.prompts)
        self.assertNotIn("🚀 How many new prospects should we aim to collect?", prompter.prompts)
        self.assertEqual(result["user_profile"], "indie_founder")
        self.assertEqual(result["pricing_model"], "freemium")
        self.assertEqual(result["revenue_mode"], "tracker")
        self.assertTrue(result["consider_mrr"])
        self.assertEqual(result["mrr_ranges"], [])
        self.assertEqual(result["categories"], ["Health & Fitness"])
        self.assertEqual(result["custom_keywords"], [])
        self.assertEqual(result["developer_type"], "solo")
        self.assertIsNone(result["max_mrr"])
        self.assertTrue(result["confirmed"])

    def test_old_audience_value_loads_as_an_icp(self):
        loaded = validate_targeting({"developer_type": "solo_small", "categories": ["Productivity"]})
        self.assertEqual(loaded["developer_type"], "solo")
        open_cap = validate_targeting(
            {"developer_type": "scaleup", "max_ratings": "any", "categories": ["Productivity"]}
        )
        self.assertEqual(open_cap["max_ratings"], "any")
        no_ceiling = validate_targeting(
            {"developer_type": "solo", "categories": ["Productivity"], "max_mrr": "any"}
        )
        self.assertIsNone(no_ceiling["max_mrr"])
        capped = validate_targeting(
            {"developer_type": "solo", "categories": ["Productivity"], "max_mrr": 10000}
        )
        self.assertEqual(capped["max_mrr"], 10000)

    def test_saved_configuration_can_be_edited_without_repeating_every_question(self):
        saved = dict(self.base)
        saved["confirmed"] = True
        saved["user_profile"] = "growth_agency"
        saved["pricing_model"] = "freemium"
        saved["consider_mrr"] = True
        prompter = ScriptedPrompter(["continue", "edit", "ratings", 10, "start"])
        result = run_wizard(prompter, saved, self.settings, self.templates, self.summary)
        self.assertEqual(result["max_ratings"], 10)
        self.assertEqual(result["platforms"], "ios")
        self.assertEqual(result["min_score"], 50)

    def test_country_prompt_marks_global_english_markets_as_recommended(self):
        prompter = ScriptedPrompter(["us"])
        result = _ask_country(prompter, {"country": "us"})
        self.assertIn("Global / Top English Markets (US, UK, CA, AU) (Recommended)", prompter.prompts[0])
        self.assertEqual(result["country"], "us")
        fresh = validate_targeting({})
        self.assertEqual(fresh["country"], "global")
        self.assertEqual(fresh["last_update"], "180d")
        self.assertEqual(fresh["max_ratings"], 250)
        self.assertEqual(fresh["developer_types"], ["solo", "startup"])
        saved_solo = validate_targeting({"developer_type": "solo", "categories": ["Productivity"], "country": "us"})
        self.assertEqual(saved_solo["developer_types"], ["solo"])
        self.assertEqual(saved_solo["country"], "us")

    def test_several_icps_keep_a_matching_lead_and_drop_the_others(self):
        agency = Lead(
            platform="ios",
            app_store_id="a",
            developer_name="Bright Agency",
            description="Client apps",
            install_count=2_000_000,
        )
        agency.developer_type = classify_developer(agency, self.settings)
        self.assertEqual(agency.developer_type, "agency")
        both = validate_targeting(
            {
                "developer_types": ["solo", "agency"],
                "categories": ["Productivity"],
                "max_ratings": "any",
                "max_app_age": "any",
                "last_update": "any",
            }
        )
        self.assertEqual(both["developer_type"], "solo")
        self.assertEqual(both["developer_types"], ["solo", "agency"])
        self.assertIsNone(hard_filter_reason(agency, both))
        solo = validate_targeting(
            {
                "developer_type": "solo",
                "categories": ["Productivity"],
                "max_ratings": "any",
                "max_app_age": "any",
                "last_update": "any",
            }
        )
        self.assertIsNotNone(hard_filter_reason(agency, solo))

    def test_cancel_on_the_summary_does_not_return_a_configuration(self):
        saved = dict(self.base)
        saved["confirmed"] = True
        saved["user_profile"] = "other"
        saved["pricing_model"] = "freemium"
        saved["consider_mrr"] = True
        result = run_wizard(ScriptedPrompter(["continue", "cancel"]), saved, self.settings, self.templates, self.summary)
        self.assertIsNone(result)

    def test_mrr_ranges_checkbox_treats_none_and_all_as_unfiltered(self):
        seen = {}

        class Recording(ScriptedPrompter):
            def checkbox(self, message, choices, default=None):
                seen["message"] = message
                seen["choices"] = list(choices)
                seen["default"] = default
                return super().checkbox(message, choices, default)

        empty = _ask_mrr_ranges(Recording([[]]), {"user_profile": "growth_agency"}, allow_back=False)
        self.assertEqual(seen["message"], "💰 MRR Ranges")
        self.assertEqual(seen["default"], [])
        self.assertIn(("< $5k MRR", "< $5k MRR"), seen["choices"])
        self.assertIn(("$5k - $10k MRR", "$5k - $10k MRR"), seen["choices"])
        self.assertIn(("$10k - $50k MRR", "$10k - $50k MRR"), seen["choices"])
        self.assertEqual(empty["mrr_ranges"], [])
        self.assertTrue(empty["consider_mrr"])
        self.assertEqual(empty["revenue_mode"], "tracker")
        self.assertIsNone(empty["max_mrr"])
        picked = _ask_mrr_ranges(
            ScriptedPrompter([["< $5k MRR", "$10k - $50k MRR"]]),
            {},
            allow_back=False,
        )
        self.assertEqual(picked["mrr_ranges"], ["< $5k MRR", "$10k - $50k MRR"])
        everything = [value for _label, value in seen["choices"]]
        all_selected = _ask_mrr_ranges(ScriptedPrompter([everything]), {}, allow_back=False)
        self.assertEqual(all_selected["mrr_ranges"], [])
        kept = filter_by_mrr_ranges(
            [
                SimpleNamespace(estimated_mrr="< $5k MRR", mrr_numeric=1000),
                SimpleNamespace(estimated_mrr="$10k - $50k MRR", mrr_numeric=20000),
                SimpleNamespace(estimated_mrr="N/A (No IAP)", mrr_numeric=0),
            ],
            picked["mrr_ranges"],
        )
        self.assertEqual([lead.estimated_mrr for lead in kept], ["< $5k MRR", "$10k - $50k MRR"])
        self.assertEqual(hunt_target(30, picked["mrr_ranges"]), 75)
        self.assertEqual(hunt_target(30, []), 30)

    def test_agency_path_offers_under_250_and_angel_hides_the_rating_menu(self):
        prompter = ScriptedPrompter([250])
        shown = []
        original = prompter.select

        def select(message, choices, default=None):
            shown.append((message, list(choices)))
            return original(message, choices, default)

        prompter.select = select
        result = _ask_ratings(prompter, {"consider_mrr": False, "developer_types": ["agency"], "max_ratings": 1000})
        self.assertEqual(result["max_ratings"], 250)
        self.assertIn(("Under 250 ratings (Recommended)", 250), shown[0][1])
        self.assertIn(("Under 10 ratings", 10), shown[0][1])
        freemium = [
            "_ask_profile",
            "_ask_pricing",
            "_ask_category",
            "_ask_mrr_ranges",
            "_ask_developer",
            "_ask_min_ratings",
            "_ask_ratings",
            "_ask_age",
            "_ask_update",
        ]
        names = [step.__name__ for step in _steps({"consider_mrr": True, "user_profile": "angel_vc", "pricing_model": "freemium"})]
        self.assertEqual(names, freemium)
        self.assertNotIn("_ask_score", names)
        self.assertNotIn("_ask_template", names)
        self.assertNotIn("_ask_volume", names)
        self.assertNotIn("_ask_country", names)
        self.assertNotIn("_ask_max_mrr", names)
        off = [step.__name__ for step in _steps({"consider_mrr": False, "user_profile": "growth_agency", "pricing_model": "freemium"})]
        self.assertEqual(off, freemium)
        internal = [step.__name__ for step in _steps({"consider_mrr": True, "user_profile": "angel_vc", "pricing_model": "freemium"}, internal=True)]
        self.assertEqual(internal, freemium)
        upfront = [step.__name__ for step in _steps({"pricing_model": "upfront", "user_profile": "angel_vc"})]
        self.assertEqual(
            upfront,
            ["_ask_profile", "_ask_pricing", "_ask_category", "_ask_developer", "_ask_min_ratings", "_ask_ratings", "_ask_age", "_ask_update"],
        )

    def test_angel_freemium_keeps_mrr_and_asks_the_rating_filters(self):
        prompter = ScriptedPrompter(
            ["angel_vc", "freemium", ["Finance"], ["< $5k MRR"], ["solo"], "any", 250, "2y", "180d", "start"]
        )
        result = run_wizard(prompter, self.base, self.settings, self.templates, self.summary)
        self.assertEqual(result["user_profile"], "angel_vc")
        self.assertEqual(result["pricing_model"], "freemium")
        self.assertEqual(result["revenue_mode"], "tracker")
        self.assertTrue(result["consider_mrr"])
        self.assertEqual(result["mrr_ranges"], ["< $5k MRR"])
        self.assertEqual(result["max_ratings"], 250)
        self.assertEqual(result["last_update"], "180d")
        self.assertEqual(result["categories"], ["Finance"])
        self.assertEqual(result["developer_types"], ["solo"])
        self.assertIsNone(result["max_mrr"])
        self.assertIn("⭐ What is the maximum number of ratings an app can have?", prompter.prompts)
        self.assertNotIn("💰 Max MRR to include:", prompter.prompts)
        self.assertNotIn("💰 Consider Estimated Revenue / MRR?", prompter.prompts)

    def test_empty_category_selection_is_unfiltered(self):
        result = _ask_category(ScriptedPrompter([[]]), {"categories": ["Finance"]}, allow_back=False)
        self.assertEqual(result["categories"], [])
        loaded = validate_targeting({"developer_type": "solo", "categories": []})
        self.assertEqual(loaded["categories"], [])
        self.assertEqual(category_plan(loaded, self.settings), list(ONBOARDING_CATEGORIES))

    def test_upfront_paid_skips_the_mrr_question(self):
        prompter = ScriptedPrompter(
            ["angel_vc", "upfront", ["Finance"], ["solo"], "any", 250, "2y", "180d", "start"]
        )
        result = run_wizard(prompter, self.base, self.settings, self.templates, self.summary)
        self.assertEqual(result["pricing_model"], "upfront")
        self.assertEqual(result["revenue_mode"], "gross_sales")
        self.assertFalse(result["consider_mrr"])
        self.assertNotIn("💰 MRR Ranges", prompter.prompts)
        self.assertNotIn("💰 Consider Estimated Revenue / MRR?", prompter.prompts)

    def test_ad_supported_skips_the_mrr_question(self):
        prompter = ScriptedPrompter(
            ["indie_founder", "ad_supported", ["Utilities"], ["solo"], "any", 250, "2y", "180d", "start"]
        )
        result = run_wizard(prompter, self.base, self.settings, self.templates, self.summary)
        self.assertEqual(result["pricing_model"], "ad_supported")
        self.assertEqual(result["revenue_mode"], "no_iap")
        self.assertFalse(result["consider_mrr"])
        self.assertEqual(result["mrr_ranges"], [])
        self.assertNotIn("💰 MRR Ranges", prompter.prompts)
        self.assertNotIn("💰 Consider Estimated Revenue / MRR?", prompter.prompts)

    def test_agency_freemium_asks_min_and_max_ratings(self):
        prompter = ScriptedPrompter(
            ["growth_agency", "freemium", ["Productivity"], [], ["agency"], "any", 250, "2y", "180d", "start"]
        )
        result = run_wizard(prompter, self.base, self.settings, self.templates, self.summary)
        self.assertEqual(result["user_profile"], "growth_agency")
        self.assertTrue(result["consider_mrr"])
        self.assertEqual(result["mrr_ranges"], [])
        self.assertEqual(result["revenue_mode"], "tracker")
        self.assertEqual(result["developer_types"], ["agency"])
        self.assertNotIn("min_ratings", result)
        self.assertEqual(result["max_ratings"], 250)
        self.assertEqual(prompter.prompts[5], "⭐ What is the minimum number of ratings an app can have?")
        self.assertEqual(prompter.prompts[6], "⭐ What is the maximum number of ratings an app can have?")

    def test_saved_run_without_a_profile_asks_the_opening_prompts_first(self):
        saved = dict(self.base)
        saved["confirmed"] = True
        prompter = ScriptedPrompter(["growth_agency", "freemium", [], "continue", "start"])
        result = run_wizard(prompter, saved, self.settings, self.templates, self.summary)
        self.assertEqual(prompter.prompts[0], "👤 What best describes you?")
        self.assertEqual(prompter.prompts[1], "💳 App Pricing Model")
        self.assertEqual(prompter.prompts[2], "💰 MRR Ranges")
        self.assertEqual(result["user_profile"], "growth_agency")
        self.assertEqual(result["pricing_model"], "freemium")
        self.assertEqual(result["revenue_mode"], "tracker")
        self.assertTrue(result["consider_mrr"])
        self.assertEqual(result["mrr_ranges"], [])
        self.assertEqual(result["platforms"], "ios")
        self.assertEqual(result["categories"], ["Productivity", "Health & Fitness"])


class PipelineTests(unittest.TestCase):
    def test_category_plan_uses_genres_and_ignores_keywords(self):
        settings = load_settings()
        targeting = validate_targeting(
            {
                "categories": ["Productivity", "Health & Fitness", "Photo & Video"],
                "custom_keywords": ["custom tracker"],
                "platforms": "both",
            }
        )
        planned = category_plan(targeting, settings)
        self.assertEqual(planned, ["Productivity", "Health & Fitness", "Photo & Video"])
        self.assertNotIn("custom tracker", planned)
        self.assertNotIn("focus timer", planned)
        jobs = search_jobs(targeting, settings)
        self.assertTrue(all(platform == "ios" for platform, _category in jobs))
        self.assertEqual([category for _platform, category in jobs], planned)

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
                "max_ratings": 100,
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
        self.assertEqual(quiet.score_normalized, 95)
        self.assertEqual(quiet.primary_email.address, "jane@quiet.example")
        self.assertEqual(quiet.primary_email.email_type, "founder")
        self.assertEqual(quiet.contact_name, "Jane Doe")
        self.assertEqual(quiet.estimated_mrr, "")
        self.assertEqual(quiet.mrr_status, "pending")
        self.assertEqual(
            next(lead for lead in discovery.missing_site if lead.app_name == "Paper Day").estimated_mrr,
            "",
        )
        self.assertEqual({lead.app_name for lead in discovery.missing_site}, {"Paper Day"})
        self.assertEqual({lead.app_name for lead in discovery.missing_email}, {"Pocket Budget"})
        self.assertEqual(stats["already_in_crm"], 1)
        self.assertGreaterEqual(stats["filtered_out"], 4)
        self.assertFalse(stats["errors"])

    def test_discovery_advances_to_the_next_page_until_the_target_is_met(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "2y",
                "last_update": "90d",
                "min_score": 50,
                "target_prospects": 1,
                "country": "us",
            }
        )
        by_name = {item["trackName"]: item for _platform, _keyword, item in sample_items()}
        calls = []

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page == 1:
                return [by_name["Giant Tasks"]], {"cached": False}
            return [by_name["Quiet Focus"]], {"cached": False}

        voice = _Capture()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=voice,
        )
        self.assertEqual(calls, [("Productivity", 1), ("Productivity", 2)])
        self.assertEqual({lead.app_name for lead in discovery.qualified}, {"Quiet Focus"})
        self.assertTrue(any("expanding search for 1 more" in line for line in voice.lines))
        self.assertTrue(any("[1/1 qualified leads found]" in line for line in voice.lines))
        self.assertTrue(any("Qualified Quiet Focus" in line for line in voice.lines))
        self.assertFalse(any("Checking MRR" in line for line in voice.lines))
        self.assertEqual(discovery.qualified[0].estimated_mrr, "")
        self.assertEqual(discovery.qualified[0].mrr_status, "pending")

    def test_a_repeated_app_is_enriched_once(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "2y",
                "last_update": "90d",
                "min_score": 50,
                "target_prospects": 1,
                "country": "us",
            }
        )
        quiet = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        seen = []

        def search(_platform, _keyword, _country, page=1):
            if page > 1:
                return [], {"cached": False}
            return [quiet, dict(quiet)], {"cached": False}

        def enrich(lead):
            seen.append(lead.app_store_id)
            return demo_enricher(settings)(lead)

        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=enrich,
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
        )
        self.assertEqual(seen, ["1001"])
        self.assertEqual(len(discovery.qualified), 1)

    def test_target_of_five_stops_enrichment_and_saves_exactly_five(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=6, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "90d",
                "min_score": 40,
                "target_prospects": 5,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        apps = []
        for offset in range(8):
            item = dict(base)
            app_id = 2001 + offset
            item["trackId"] = app_id
            item["trackName"] = f"Quiet Focus {offset}"
            item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
            apps.append(item)
        calls = []

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page > 1:
                return [], {"cached": False}
            return list(apps), {"cached": False}

        crawled = []

        def enrich(lead):
            crawled.append(lead.app_store_id)
            return demo_enricher(settings)(lead)

        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=enrich,
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
        )
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual(len(crawled), 5)
        self.assertEqual(len(discovery.qualified), 5)
        self.assertEqual(discovery.target, 5)
        stats = discovery.stats
        self.assertEqual(stats["discovered"], 8)
        self.assertEqual(stats["qualified"], 5)
        self.assertEqual(stats["filtered_out"], 3)
        accounted = (
            stats["already_in_crm"]
            + stats["filtered_out"]
            + stats["missing_developer_site"]
            + stats["missing_public_email"]
            + stats["qualified"]
        )
        self.assertEqual(accounted, stats["discovered"])
        self.assertFalse(stats["errors"])

    def test_max_mrr_drops_known_high_apps_and_keeps_blanks(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=6, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "90d",
                "min_score": 40,
                "target_prospects": 2,
                "max_mrr": 10000,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        apps = []
        for offset in range(4):
            item = dict(base)
            app_id = 2001 + offset
            item["trackId"] = app_id
            item["trackName"] = f"Quiet Focus {offset}"
            item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
            apps.append(item)
        calls = []

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page > 1:
                return [], {"cached": False}
            return list(apps), {"cached": False}

        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
            demo=True,
        )
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual([lead.app_name for lead in discovery.qualified], ["Quiet Focus 0", "Quiet Focus 1"])
        self.assertTrue(all(lead.estimated_mrr == "" for lead in discovery.qualified))
        self.assertTrue(all(lead.mrr_status == "pending" for lead in discovery.qualified))
        self.assertTrue(all(lead.mrr_numeric is None for lead in discovery.qualified))
        stats = discovery.stats
        self.assertEqual(stats["discovered"], 4)
        self.assertEqual(stats["qualified"], 2)
        self.assertEqual(stats["filtered_out"], 2)
        self.assertEqual(stats["rejections"]["mrr_exceeded"], 0)
        self.assertFalse(stats["errors"])

    def test_blank_mrr_still_completes_scoring_and_the_sheet_phase(self):
        from main import _review

        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 1,
                "country": "us",
            }
        )
        quiet = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")

        def search(_platform, _keyword, _country, page=1):
            if page > 1:
                return [], {"cached": False}
            return [quiet], {"cached": False}

        def getter(url, headers=None, timeout=None):
            body = "<html><head><title>Quiet Focus 1001</title></head><body><dt>Revenue</dt><dd>-</dd></body></html>"
            return SimpleNamespace(status_code=200, text=body, url=url)

        from modules.pipeline import analyze_saved
        from modules.run_store import RunStore

        board = AgentDashboard(1, enabled=False)
        board.begin_clock()
        board.focus(1, "Indexing existing CRM leads...")
        board.complete(1, "Indexed 0 leads")
        store = RunStore("blank-mrr")
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
            demo=False,
            dashboard=board,
            on_saved=store.save,
        )
        self.assertEqual(len(discovery.qualified), 1)
        self.assertEqual(discovery.qualified[0].estimated_mrr, "")
        self.assertEqual(discovery.qualified[0].mrr_status, "pending")
        self.assertIsNone(discovery.qualified[0].mrr_numeric)
        self.assertEqual(board.phases[1]["state"], "complete")
        self.assertEqual(board.phases[2]["state"], "complete")
        self.assertEqual(board.phases[3]["state"], "complete")
        self.assertEqual(board.phases[4]["state"], "pending")
        self.assertEqual(board.funnel["qualified_count"], 1)
        analyze_saved(store, demo=False, getter=getter, dashboard=board, narrator=_Capture())
        self.assertEqual(discovery.qualified[0].estimated_mrr, "")
        self.assertEqual(board.phases[2]["state"], "complete")
        self.assertEqual(board.phases[3]["state"], "complete")
        self.assertEqual(board.phases[4]["state"], "complete")
        self.assertEqual(board.funnel["qualified_count"], 1)
        self.assertEqual(board.phases[5]["state"], "pending")
        templates = load_templates(settings)
        code = _review(
            discovery,
            None,
            templates,
            settings,
            ScriptedPrompter(["all", "import"]),
            None,
            None,
            demo=True,
            dashboard=board,
        )
        self.assertEqual(code, 0)
        self.assertEqual(board.phases[5]["state"], "complete")

    def test_next_search_starts_while_the_first_batch_is_still_enriching(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=2, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 2,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        apps = []
        for offset, app_id in enumerate((2102, 2104)):
            item = dict(base)
            item["trackId"] = app_id
            item["trackName"] = f"Quiet Focus {offset}"
            item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
            apps.append(item)
        calls = []
        in_page2 = threading.Event()
        release_page2 = threading.Event()
        page2_done = threading.Event()
        overlap = {"during_enrich": False}

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page == 1:
                return [apps[0]], {"cached": False}
            in_page2.set()
            release_page2.wait(3)
            page2_done.set()
            return [apps[1]], {"cached": False}

        def enrich(lead):
            if lead.app_store_id == "2102" and not overlap["during_enrich"]:
                entered = in_page2.wait(2)
                overlap["during_enrich"] = entered and not page2_done.is_set()
                release_page2.set()
            return demo_enricher(settings)(lead)

        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=enrich,
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
            demo=True,
        )
        self.assertEqual(calls, [("Productivity", 1), ("Productivity", 2)])
        self.assertTrue(overlap["during_enrich"])
        self.assertEqual(len(discovery.qualified), 2)
        self.assertFalse(discovery.stats["errors"])

    def test_scouting_does_not_fetch_mrr_while_the_next_chart_is_in_flight(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=4, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 2,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        apps = []
        for offset, app_id in enumerate((2201, 2202)):
            item = dict(base)
            item["trackId"] = app_id
            item["trackName"] = f"Quiet Focus {offset}"
            item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
            apps.append(item)
        calls = []
        tracker_calls = []

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page == 1:
                return [apps[0]], {"cached": False}
            return [apps[1]], {"cached": False}

        def getter(url, headers=None, timeout=None):
            tracker_calls.append(url)
            raise AssertionError("scouting must not call the revenue provider")

        voice = _Capture()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=voice,
            demo=False,
            mrr_getter=getter,
        )
        self.assertEqual(calls, [("Productivity", 1), ("Productivity", 2)])
        self.assertEqual(tracker_calls, [])
        self.assertEqual(len(discovery.qualified), 2)
        self.assertTrue(all(lead.mrr_status == "pending" for lead in discovery.qualified))
        self.assertTrue(all(lead.estimated_mrr == "" for lead in discovery.qualified))
        self.assertFalse(any("No public MRR listed" in line for line in voice.lines))
        self.assertFalse(discovery.stats["errors"])

    def test_rate_limited_mrr_keeps_the_lead_and_does_not_search_again(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=4, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 1,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        calls = []

        def search(_platform, keyword, _country, page=1):
            calls.append((keyword, page))
            if page > 1:
                return [dict(base, trackId=2999, trackName="Extra App")], {"cached": False}
            return [base], {"cached": False}

        def getter(url, headers=None, timeout=None):
            return SimpleNamespace(status_code=429, text="Too Many Requests", url=url)

        voice = _Capture()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=voice,
            demo=False,
            mrr_getter=getter,
        )
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual(len(discovery.qualified), 1)
        self.assertEqual(discovery.qualified[0].estimated_mrr, "")
        self.assertEqual(discovery.qualified[0].mrr_status, "pending")
        self.assertIsNone(discovery.qualified[0].mrr_numeric)
        self.assertFalse(any("keeping lead" in line for line in voice.lines))
        self.assertFalse(discovery.stats["errors"])

    def test_parallel_waves_finish_fast_and_stop_at_the_target(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 24,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        apps = []
        for offset in range(24):
            item = dict(base)
            app_id = 3000 + offset
            item["trackId"] = app_id
            item["trackName"] = f"Quiet Focus {offset}"
            item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
            apps.append(item)
        state = {"current": 0, "max": 0}
        lock = threading.Lock()
        crawled = []

        def search(_platform, _keyword, _country, page=1):
            if page > 1:
                return [], {"cached": False}
            return list(apps), {"cached": False}

        def enrich(lead):
            with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
                crawled.append(lead.app_store_id)
            time.sleep(0.25)
            with lock:
                state["current"] -= 1
            return demo_enricher(settings)(lead)

        started = time.monotonic()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=enrich,
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
            demo=True,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(len(crawled), 24)
        self.assertEqual(len(discovery.qualified), 24)
        self.assertGreaterEqual(state["max"], 8)
        self.assertLess(elapsed, 3.0)
        self.assertFalse(discovery.stats["errors"])

    def test_update_window_relaxation_recovers_stashed_leads(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "90d",
                "min_score": 40,
                "target_prospects": 2,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        fresh = dict(base)
        fresh["trackId"] = 4102
        fresh["trackName"] = "Fresh Focus"
        fresh["currentVersionReleaseDate"] = (utc_today() - timedelta(days=10)).isoformat() + "T00:00:00Z"
        fresh["trackViewUrl"] = "https://apps.apple.com/us/app/fresh-focus/id4102"
        stale = dict(base)
        stale["trackId"] = 4104
        stale["trackName"] = "Stale Focus"
        stale["currentVersionReleaseDate"] = (utc_today() - timedelta(days=120)).isoformat() + "T00:00:00Z"
        stale["trackViewUrl"] = "https://apps.apple.com/us/app/stale-focus/id4104"
        calls = []

        def search(_platform, keyword, country, page=1):
            calls.append((keyword, page, country))
            if country == "us" and page == 1:
                return [fresh, stale], {"cached": False}
            return [], {"cached": False}

        voice = _Capture()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=voice,
            demo=True,
        )
        self.assertEqual({lead.app_name for lead in discovery.qualified}, {"Fresh Focus", "Stale Focus"})
        self.assertTrue(any("Widening storefront & keyword net to hit 2/2 target..." in line for line in voice.lines))
        self.assertTrue(all(country == "us" for _keyword, _page, country in calls))
        self.assertFalse(discovery.stats["errors"])
        stats = discovery.stats
        accounted = (
            stats["already_in_crm"]
            + stats["filtered_out"]
            + stats["missing_developer_site"]
            + stats["missing_public_email"]
            + stats["qualified"]
        )
        self.assertEqual(accounted, stats["discovered"])

    def test_rating_ceiling_relaxes_to_one_and_a_half_times(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        settings["category_keywords"] = {"Productivity": ["focus timer"]}
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 60,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 1,
                "country": "us",
            }
        )
        base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
        near = dict(base)
        near["trackId"] = 4202
        near["trackName"] = "Near Cap"
        near["userRatingCount"] = 70
        near["trackViewUrl"] = "https://apps.apple.com/us/app/near-cap/id4202"
        far = dict(base)
        far["trackId"] = 4204
        far["trackName"] = "Far Cap"
        far["userRatingCount"] = 120
        far["trackViewUrl"] = "https://apps.apple.com/us/app/far-cap/id4204"

        def search(_platform, _keyword, country, page=1):
            if country == "us" and page == 1:
                return [near, far], {"cached": False}
            return [], {"cached": False}

        voice = _Capture()
        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=voice,
            demo=True,
        )
        self.assertEqual({lead.app_name for lead in discovery.qualified}, {"Near Cap"})
        self.assertEqual(discovery.stats["rejections"]["ratings_above_cap"], 1)
        self.assertTrue(any("Widening storefront & keyword net to hit 1/1 target..." in line for line in voice.lines))
        self.assertFalse(discovery.stats["errors"])

    def test_a_short_chart_run_searches_the_category_phrases(self):
        settings = load_settings()
        settings["apify"] = dict(settings["apify"], max_searches_per_run=1, end_page=1)
        scoring = load_scoring(settings)
        targeting = validate_targeting(
            {
                "developer_type": "solo",
                "platforms": "ios",
                "categories": ["Productivity"],
                "max_ratings": 100,
                "max_app_age": "any",
                "last_update": "any",
                "min_score": 40,
                "target_prospects": 1,
                "country": "us",
            }
        )
        by_name = {item["trackName"]: item for _platform, _keyword, item in sample_items()}
        calls = []

        def search(_platform, keyword, country, page=1):
            calls.append((keyword, country, page))
            if keyword == "focus timer" and country == "us":
                return [by_name["Quiet Focus"]], {"cached": False}
            if keyword == "Productivity" and country == "us" and page == 1:
                return [by_name["Giant Tasks"]], {"cached": False}
            return [], {"cached": False}

        discovery = discover(
            targeting,
            settings,
            scoring,
            DedupIndex(),
            searcher=search,
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            narrator=_Capture(),
            demo=True,
        )
        self.assertEqual({lead.app_name for lead in discovery.qualified}, {"Quiet Focus"})
        self.assertIn(("focus timer", "us", 1), calls)
        self.assertGreaterEqual(discovery.stats["rejections"]["ratings_above_cap"], 1)
        self.assertFalse(discovery.stats["errors"])

    def test_summary_names_the_largest_hard_filter(self):
        text = format_summary(
            {
                "discovered": 10,
                "already_in_crm": 0,
                "filtered_out": 8,
                "missing_developer_site": 1,
                "missing_public_email": 1,
                "qualified": 0,
                "filtered_hard": 8,
                "rejections": {"ratings_above_cap": 6, "pricing": 2, "no_website": 1, "no_email_found": 1},
            }
        )
        self.assertIn("Hard filters: 8", text)
        self.assertIn("ratings cap: 6", text)
        self.assertIn("pricing filter: 2", text)
        self.assertNotIn("no developer website: 1", text)


class CategoryFeedTests(unittest.TestCase):
    def test_rss_ids_are_looked_up_without_a_keyword(self):
        self.assertEqual(
            chart_url("us", 6013, "topfreeapplications"),
            "https://itunes.apple.com/us/rss/topfreeapplications/limit=200/genre=6013/json",
        )
        self.assertEqual(
            chart_url("gb", 6007, "toppaidapplications"),
            "https://itunes.apple.com/gb/rss/toppaidapplications/limit=200/genre=6007/json",
        )
        feed = {
            "feed": {
                "entry": [
                    {"id": {"attributes": {"im:id": "1001"}}},
                    {"id": {"label": "https://apps.apple.com/us/app/quiet-focus/id1002"}},
                ]
            }
        }
        self.assertEqual(entry_ids(feed), ["1001", "1002"])
        calls = []

        def getter(url: str) -> dict:
            calls.append(url)
            if "/rss/" in url:
                self.assertNotIn("term=", url)
                self.assertIn("genre=6013", url)
                return feed
            self.assertTrue(url.startswith("https://itunes.apple.com/lookup?id=1001,1002"))
            self.assertIn("country=us", url)
            return {
                "results": [
                    {
                        "kind": "software",
                        "trackId": 1001,
                        "trackName": "Quiet Focus",
                        "sellerUrl": "https://quiet.example",
                        "supportUrl": "https://quiet.example/support",
                        "trackViewUrl": "https://apps.apple.com/us/app/quiet-focus/id1001",
                        "artistName": "Jane Doe",
                        "userRatingCount": 8,
                        "formattedPrice": "Free",
                    },
                    {"wrapperType": "artist", "artistId": 9},
                ]
            }

        apps = fetch_genre_apps("Health & Fitness", "us", "topfreeapplications", getter)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["sellerUrl"], "https://quiet.example")
        self.assertEqual(apps[0]["supportUrl"], "https://quiet.example/support")
        self.assertIn("formattedPrice", apps[0])
        self.assertTrue(lookup_url("us", ["1001"]).startswith("https://itunes.apple.com/lookup?id=1001"))
        self.assertTrue(all("focus" not in url and "keyword" not in url for url in calls))

    def test_a_phrase_uses_itunes_search_instead_of_a_chart(self):
        calls = []

        def getter(url: str) -> dict:
            calls.append(url)
            return {
                "results": [
                    {
                        "kind": "software",
                        "trackId": 50,
                        "trackName": "Habit Grid",
                        "userRatingCount": 80,
                        "sellerUrl": "https://habit.example",
                    }
                ]
            }

        search = category_searcher({}, getter)
        items, meta = search("ios", "habit tracker", "us", 1)
        self.assertEqual(items[0]["trackName"], "Habit Grid")
        self.assertEqual(items[0]["sellerUrl"], "https://habit.example")
        self.assertIn("/search?term=habit+tracker", calls[0])
        self.assertIn("country=us", calls[0])
        self.assertNotIn("/rss/", calls[0])
        self.assertNotIn("/lookup", calls[0])
        self.assertEqual(meta["usage_usd"], 0)


class SaveCapTests(unittest.TestCase):
    def test_review_slices_the_sheet_payload_to_the_target(self):
        from main import _review
        from modules.pipeline import Discovery, empty_stats

        settings = load_settings()
        templates = load_templates(settings)
        leads = [Lead(platform="ios", app_store_id=str(3000 + index), app_name=f"App {index}") for index in range(8)]
        discovery = Discovery(qualified=leads, stats=empty_stats(), target=5)
        saved = {}

        class _Sheet:
            def append_leads(self, rows, template, suppressed):
                del template, suppressed
                saved["rows"] = list(rows)
                return len(rows), 0

        code = _review(
            discovery,
            _Sheet(),
            templates,
            settings,
            ScriptedPrompter(["all", "import"]),
            None,
            None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(saved["rows"]), 5)


class _Capture(Narrator):
    def __init__(self):
        self.lines: list[str] = []
        self.console = None

    def line(self, message: str) -> None:
        self.lines.append(message)

    def spin(self, message: str):
        self.line(message)
        return nullcontext()


if __name__ == "__main__":
    unittest.main()
