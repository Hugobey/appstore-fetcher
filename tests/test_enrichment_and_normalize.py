import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.config import load_settings
from modules.enrichment import (
    classify_email,
    decode_cloudflare_email,
    enrich_lead,
    extract_contact_name,
    extract_emails,
    is_public_url,
)
from modules.models import Lead
from modules.scraper import apply_itunes_record, normalize_app


IOS_ITEM = {
    "kind": "software",
    "trackId": "389801252",
    "trackName": "Sample Photo App",
    "artistName": "Sample Inc.",
    "trackViewUrl": "https://apps.apple.com/us/app/sample-photo-app/id389801252?uo=4",
    "sellerUrl": "https://www.example.com",
    "primaryGenreName": "Photo & Video",
    "releaseDate": "2018-04-02T07:00:00Z",
    "currentVersionReleaseDate": "2026-08-20T07:00:00Z",
    "price": 0,
    "userRatingCount": 12,
    "averageUserRating": 4.5,
    "reviews": [{"id": "1", "review": "Nice"}],
}

ANDROID_ITEM = {
    "title": "Hello Neighbor",
    "appId": "com.tinybuildgames.helloneighbor",
    "url": "https://play.google.com/store/apps/details?id=com.tinybuildgames.helloneighbor&hl=en",
    "summary": "A stealth game.",
    "ratings": 10,
    "reviews": 20762,
    "free": True,
    "price": 0,
    "developer": "tinyBuild Games",
    "developerEmail": "support@tinybuild.com",
    "genre": "Action",
    "released": "Dec 6, 2018",
    "updated": 1749574800000,
}


class NormalizeTests(unittest.TestCase):
    def test_ios_ratings_are_not_taken_from_the_review_array(self):
        lead = normalize_app(IOS_ITEM, "ios", "photo")
        self.assertEqual(lead.app_store_id, "389801252")
        self.assertEqual(lead.rating_count, 12)
        self.assertIsNone(lead.review_count)
        self.assertEqual(lead.app_store_url, "https://apps.apple.com/us/app/id389801252")
        self.assertEqual(lead.developer_website, "https://www.example.com")
        self.assertIsNone(lead.install_count)
        self.assertIsNone(lead.revenue_usd)
        self.assertIsNone(normalize_app({"error": True, "code": "C003", "message": "missing"}, "ios"))

    def test_android_installs_and_revenue_are_kept_when_the_actor_sends_them(self):
        item = dict(ANDROID_ITEM)
        item["installs"] = "50,000+"
        item["minInstalls"] = 50000
        item["estimatedRevenue"] = "$1.2M"
        lead = normalize_app(item, "android", "neighbor")
        self.assertEqual(lead.install_count, 50000)
        self.assertEqual(lead.revenue_usd, 1_200_000)
        text_only = dict(ANDROID_ITEM)
        text_only["installs"] = "100,000+"
        parsed = normalize_app(text_only, "android", "neighbor")
        self.assertEqual(parsed.install_count, 100000)
        self.assertIsNone(parsed.revenue_usd)

    def test_android_keeps_ratings_and_written_reviews_apart(self):
        lead = normalize_app(ANDROID_ITEM, "android", "neighbor")
        self.assertEqual(lead.platform, "android")
        self.assertEqual(lead.rating_count, 10)
        self.assertEqual(lead.review_count, 20762)
        self.assertEqual(lead.emails[0].address, "support@tinybuild.com")
        self.assertEqual(lead.emails[0].email_type, "support")
        self.assertEqual(lead.release_date, "2018-12-06")
        self.assertEqual(lead.last_update, "2025-06-10")

    def test_itunes_fills_gaps_only(self):
        lead = normalize_app(IOS_ITEM, "ios")
        lead.description = ""
        apply_itunes_record(
            lead,
            {
                "description": "A photo app.",
                "sellerUrl": "https://other.example",
                "userRatingCount": 99,
                "primaryGenreName": "Utilities",
            },
        )
        self.assertEqual(lead.description, "A photo app.")
        self.assertEqual(lead.developer_website, "https://www.example.com")
        self.assertEqual(lead.rating_count, 12)
        self.assertEqual(lead.category, "Photo & Video")


class EnrichmentTests(unittest.TestCase):
    def test_extracts_public_emails_and_ignores_placeholders(self):
        encoded = "a8" + "".join(f"{ord(char) ^ 0xA8:02x}" for char in "jane@quiet.example")
        html = """
        <html><body>
          <a href="mailto:jane@quiet.example">mail</a>
          <p>Founder: Jane Doe</p>
          <span data-cfemail="%s"></span>
          <p>user@example.com</p>
          <p>sprite@2x.png</p>
          <p>hello [at] quiet [dot] example</p>
          <script>var x = "secret@quiet.example";</script>
        </body></html>
        """ % encoded
        emails = extract_emails(html)
        self.assertIn("jane@quiet.example", emails)
        self.assertIn("hello@quiet.example", emails)
        self.assertNotIn("user@example.com", emails)
        self.assertNotIn("secret@quiet.example", emails)
        self.assertEqual(decode_cloudflare_email(encoded), "jane@quiet.example")
        self.assertEqual(extract_contact_name("Founder: Jane Doe"), "Jane Doe")

    def test_founder_requires_evidence(self):
        text = "Quiet Focus is made by Jane Doe, founder. jane@quiet.example"
        kind, evidence = classify_email("jane@quiet.example", text, "Jane Doe", "solo")
        self.assertEqual(kind, "founder")
        self.assertIn("founder", evidence)
        support, _reason = classify_email("support@quiet.example", "Email support@quiet.example", "Jane Doe", "solo")
        self.assertEqual(support, "support")
        general, _reason = classify_email("hello@quiet.example", "Contact hello@quiet.example", "Jane Doe", "solo")
        self.assertEqual(general, "general")

    def test_name_match_is_not_enough_for_a_generic_mailbox(self):
        kind, _reason = classify_email("jane.doe@quiet.example", "Team page", "Jane Doe", "solo")
        self.assertEqual(kind, "founder")
        kind, _reason = classify_email("jane.doe@quiet.example", "Team page", "Jane Doe", "unknown")
        self.assertEqual(kind, "general")

    def test_robots_and_private_hosts_are_respected(self):
        self.assertFalse(is_public_url("http://127.0.0.1/secret"))
        self.assertFalse(is_public_url("http://169.254.169.254/latest"))
        self.assertFalse(is_public_url("file:///etc/passwd"))
        settings = load_settings()
        pages = {
            "https://quiet.example/robots.txt": (200, "User-agent: *\nDisallow: /\n", "https://quiet.example/robots.txt"),
        }

        def fetch(url):
            return pages.get(url, (200, "<p>jane@quiet.example</p>", url))

        lead = Lead(
            platform="ios",
            app_store_id="1",
            app_name="Quiet",
            developer_name="Jane Doe",
            developer_website="https://quiet.example",
            developer_type="solo",
        )
        enrich_lead(lead, settings, fetcher=fetch, sleep=lambda _s: None, public_check=lambda _url: True)
        self.assertEqual(lead.enrichment_status, "blocked")
        self.assertEqual(lead.emails, [])

    def test_contact_page_email_is_kept_when_homepage_has_none(self):
        settings = load_settings()
        pages = {
            "https://habits.example": (200, "<a href='/contact'>Contact</a>", "https://habits.example"),
            "https://habits.example/contact": (200, "<p>support@habits.example</p>", "https://habits.example/contact"),
        }

        def fetch(url):
            if url.endswith("/robots.txt"):
                return 404, None, url
            return pages.get(url.split("?")[0].rstrip("/"), (404, None, url))

        lead = Lead(
            platform="ios",
            app_store_id="2",
            app_name="Habits",
            developer_website="https://habits.example",
            developer_type="small_team",
        )
        enrich_lead(lead, settings, fetcher=fetch, sleep=lambda _s: None, public_check=lambda _url: True)
        self.assertEqual(lead.enrichment_status, "ok")
        self.assertEqual(lead.primary_email.address, "support@habits.example")
        self.assertEqual(lead.primary_email.email_type, "support")


if __name__ == "__main__":
    unittest.main()
