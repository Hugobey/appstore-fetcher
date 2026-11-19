import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.config import load_scoring, load_settings
from modules.models import EmailFound, Lead
from modules.scoring import (
    apply_score,
    bayesian_score,
    classify_developer,
    hard_filter_reason,
    qualification_failure,
    quality_filter_reason,
    score_lead,
)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings()
        self.scoring = load_scoring(self.settings)
        self.today = date(2026, 11, 11)

    def _lead(self, **kwargs) -> Lead:
        released = (self.today - timedelta(days=kwargs.pop("age", 40))).isoformat()
        updated = (self.today - timedelta(days=kwargs.pop("updated", 10))).isoformat()
        lead = Lead(
            platform="ios",
            app_store_id=kwargs.pop("app_store_id", "1"),
            app_name=kwargs.pop("app_name", "Sample"),
            developer_name=kwargs.pop("developer_name", "Jane Doe"),
            description=kwargs.pop("description", "Built by a solo developer."),
            release_date=released,
            last_update=updated,
            rating_count=kwargs.pop("rating_count", 8),
            average_rating=kwargs.pop("average_rating", None),
            category=kwargs.pop("category", ""),
            price=kwargs.pop("price", 0),
            has_iap=kwargs.pop("has_iap", False),
            enrichment_status=kwargs.pop("enrichment_status", "ok"),
            developer_website=kwargs.pop("developer_website", "https://example.com"),
        )
        lead.developer_type = classify_developer(lead, self.settings)
        if kwargs.pop("founder", True):
            lead.emails.append(EmailFound("jane@example.com", "founder", "https://example.com/privacy", "founder"))
        return lead

    def test_full_score_reaches_100(self):
        lead = self._lead()
        score = score_lead(lead, self.scoring, self.today)
        self.assertEqual(score.points, 100)
        self.assertEqual(score.normalized, 100)
        self.assertEqual(score.missing, [])

    def test_unknown_rating_is_excluded_from_the_denominator(self):
        lead = self._lead(rating_count=None, founder=False)
        lead.rating_count = None
        lead.review_count = 400
        score = score_lead(lead, self.scoring, self.today)
        rating = next(item for item in score.results if item.id == "ratings")
        self.assertEqual(rating.status, "unknown")
        self.assertEqual(rating.points, 0)
        self.assertNotIn(rating.max_points, [score.known_max])
        self.assertLess(score.known_max, 100)
        self.assertIn("Rating count", score.missing)
        self.assertIn("written reviews", rating.detail.lower())

    def test_disabled_criterion_does_not_count(self):
        scoring = load_scoring(self.settings)
        for criterion in scoring["criteria"]:
            if criterion["id"] == "founder_contact":
                criterion["enabled"] = False
        lead = self._lead(founder=False)
        score = score_lead(lead, scoring, self.today)
        self.assertTrue(all(item.id != "founder_contact" or item.status == "disabled" for item in score.results))
        self.assertEqual(score.known_max, 95)

    def test_many_ratings_are_a_known_miss_not_unknown(self):
        lead = self._lead(rating_count=80, founder=False)
        score = score_lead(lead, self.scoring, self.today)
        rating = next(item for item in score.results if item.id == "ratings")
        self.assertEqual(rating.status, "not_matched")
        self.assertEqual(score.known_max, 100)

    def test_thin_score_does_not_pass(self):
        lead = self._lead()
        score = score_lead(lead, self.scoring, self.today)
        score.known_max = 30
        score.normalized = 100
        reason = qualification_failure(score, 50, 40)
        self.assertIn("30", reason)

    def test_company_suffix_on_a_person_stays_solo(self):
        lead = Lead(platform="ios", app_store_id="2", developer_name="Jane Doe LLC", description="")
        self.assertEqual(classify_developer(lead, self.settings), "solo")

    def test_brand_llc_is_a_company_and_large_publishers_are_large(self):
        brand = Lead(platform="ios", app_store_id="3", developer_name="Acme Apps LLC", description="")
        large = Lead(platform="ios", app_store_id="4", developer_name="Spotify", description="")
        self.assertEqual(classify_developer(brand, self.settings), "startup")
        self.assertEqual(classify_developer(large, self.settings), "large")

    def test_hard_filters_use_ratings_not_reviews_and_keep_unknowns(self):
        targeting = {
            "max_ratings": 30,
            "max_app_age": "2y",
            "last_update": "90d",
            "developer_type": "solo_small",
        }
        too_many = self._lead(rating_count=31)
        self.assertIn("ratings", hard_filter_reason(too_many, targeting, self.today))
        reviews_only = self._lead(rating_count=None, has_iap=None)
        reviews_only.rating_count = None
        reviews_only.review_count = 5000
        self.assertIsNone(hard_filter_reason(reviews_only, targeting, self.today))
        company = self._lead(
            developer_name="Acme Apps LLC",
            description="Task app",
            rating_count=60,
            has_iap=True,
            average_rating=4.6,
        )
        company.developer_type = classify_developer(company, self.settings)
        company_targeting = dict(targeting, max_ratings="any")
        self.assertIn("Startups", hard_filter_reason(company, company_targeting, self.today))
        unknown = self._lead(developer_name="Notion", description="", rating_count=None, has_iap=None)
        unknown.rating_count = None
        unknown.developer_type = classify_developer(unknown, self.settings)
        self.assertEqual(unknown.developer_type, "unknown")
        self.assertIsNone(hard_filter_reason(unknown, targeting, self.today))

    def test_minimum_ratings_exclude_only_known_counts_below_the_floor(self):
        targeting = {
            "max_ratings": "any",
            "max_app_age": "any",
            "last_update": "any",
            "developer_type": "solo",
            "min_ratings": 50,
        }
        low = self._lead(rating_count=12)
        self.assertIn("below the minimum", hard_filter_reason(low, targeting, self.today))
        missing = self._lead(rating_count=None, has_iap=None)
        self.assertIsNone(hard_filter_reason(missing, targeting, self.today))
        enough = self._lead(rating_count=50, has_iap=True, average_rating=4.2)
        self.assertIsNone(hard_filter_reason(enough, targeting, self.today))

    def test_traction_assigns_the_four_icps(self):
        agency = Lead(
            platform="android",
            app_store_id="a",
            developer_name="Bright Agency",
            description="Client apps",
            install_count=2_000_000,
        )
        self.assertEqual(classify_developer(agency, self.settings), "agency")
        self.assertIn("agency", agency.icp_reason.lower())

        scaled = Lead(
            platform="android",
            app_store_id="b",
            developer_name="Jane Doe",
            description="A focus timer.",
            install_count=250_000,
            rating_count=12,
        )
        self.assertEqual(classify_developer(scaled, self.settings), "scaleup")
        self.assertIn("250,000", scaled.icp_reason)

        growing = Lead(platform="ios", app_store_id="c", developer_name="Jane Doe", rating_count=800)
        self.assertEqual(classify_developer(growing, self.settings), "startup")
        self.assertIn("did not send downloads", growing.icp_reason)

        rich = Lead(
            platform="ios",
            app_store_id="d",
            developer_name="Jane Doe",
            rating_count=4,
            revenue_usd=600_000,
        )
        self.assertEqual(classify_developer(rich, self.settings), "scaleup")
        self.assertIn("600,000", rich.icp_reason)

        early = Lead(platform="ios", app_store_id="e", developer_name="Jane Doe", rating_count=8)
        self.assertEqual(classify_developer(early, self.settings), "solo")

        studio = Lead(
            platform="ios",
            app_store_id="f",
            developer_name="Northwind Studio",
            description="We are a small team of two.",
            rating_count=21,
        )
        self.assertEqual(classify_developer(studio, self.settings), "startup")

        popular = Lead(platform="ios", app_store_id="g", developer_name="Spotify", install_count=5_000_000)
        self.assertEqual(classify_developer(popular, self.settings), "large")

    def test_scaleup_score_uses_installs_and_an_open_rating_cap(self):
        lead = self._lead(
            rating_count=8000,
            developer_name="Jane Doe",
            description="A focus timer.",
            has_iap=True,
            average_rating=4.5,
        )
        lead.install_count = 250_000
        lead.developer_type = classify_developer(lead, self.settings)
        self.assertEqual(lead.developer_type, "scaleup")
        score = score_lead(lead, self.scoring, self.today, icp="scaleup")
        rating = next(item for item in score.results if item.id == "ratings")
        self.assertEqual(rating.status, "awarded")
        self.assertIn("installs", rating.detail.lower())
        targeting = {
            "max_ratings": "any",
            "max_app_age": "2y",
            "last_update": "90d",
            "developer_type": "scaleup",
        }
        self.assertIsNone(hard_filter_reason(lead, targeting, self.today))

    def test_apply_score_stores_a_breakdown(self):
        lead = self._lead()
        apply_score(lead, self.scoring, self.today)
        self.assertEqual(lead.score_normalized, 100)
        self.assertTrue(lead.score_breakdown)

    def test_bayesian_score_pulls_sparse_apps_toward_the_baseline(self):
        self.assertEqual(bayesian_score(4.6, 100), 4.4)
        self.assertEqual(bayesian_score(5.0, 300), 4.8)
        self.assertIsNone(bayesian_score(None, 200))
        self.assertIsNone(bayesian_score(4.8, None))

    def test_quality_filter_drops_corporations_portfolios_and_weak_apps(self):
        indie = self._lead(rating_count=80, has_iap=True, average_rating=4.6, developer_name="Jane Doe")
        targeting = {"max_ratings": "any", "max_app_age": "any", "last_update": "any", "developer_type": "solo"}
        self.assertIsNone(hard_filter_reason(indie, targeting, self.today))
        bank = self._lead(rating_count=800, has_iap=True, average_rating=4.4, developer_name="AXA France SA")
        self.assertIn("legacy corporation", quality_filter_reason(bank))
        self.assertIsNone(quality_filter_reason(self._lead(developer_name="Northwind GmbH", rating_count=80, has_iap=True, average_rating=4.5)))
        self.assertIsNone(quality_filter_reason(self._lead(developer_name="Acme SAS", rating_count=80, has_iap=True, average_rating=4.5)))
        self.assertIn("legacy corporation", quality_filter_reason(self._lead(developer_name="Helvetia Insurance Ltd", rating_count=80, has_iap=True, average_rating=4.5)))
        portfolio = self._lead(rating_count=80, has_iap=True, average_rating=4.6, developer_name="Regional Tools")
        portfolio.seller_app_count = 16
        self.assertIn("portfolio cap", quality_filter_reason(portfolio))
        games = self._lead(rating_count=80, has_iap=True, average_rating=4.6, developer_name="Regional Tools")
        games.category = "Games"
        games.seller_app_count = 16
        self.assertIsNone(quality_filter_reason(games))
        weak = self._lead(rating_count=80, has_iap=True, average_rating=3.2)
        self.assertIn("3.2", quality_filter_reason(weak))
        thin = self._lead(rating_count=40, has_iap=True, average_rating=4.8)
        self.assertIn("50 rating floor", quality_filter_reason(thin))
        free = self._lead(rating_count=80, has_iap=False, average_rating=4.8)
        self.assertIn("no in-app purchases", quality_filter_reason(free))
        unknown_iap = self._lead(rating_count=80, has_iap=None, average_rating=4.8)
        self.assertIsNone(quality_filter_reason(unknown_iap))


if __name__ == "__main__":
    unittest.main()
