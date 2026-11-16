import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.crm import (
    BASE_HEADERS,
    BLANK_HEADERS,
    build_import_row,
    find_header_row,
    headers_to_add,
    install_blank_headers,
    safe_cell,
)
from modules.deduplication import DedupIndex, canonical_url, identity_from_url, index_url
from modules.models import EmailFound, Lead
from modules.outreach import render_template


class DedupTests(unittest.TestCase):
    def test_apple_and_play_identities(self):
        self.assertEqual(
            identity_from_url("https://apps.apple.com/fr/app/quiet-focus/id1001?uo=4"),
            ("ios", "1001"),
        )
        self.assertEqual(
            identity_from_url("https://play.google.com/store/apps/details?id=com.example.app&hl=en"),
            ("android", "com.example.app"),
        )
        self.assertEqual(
            identity_from_url("https://apps.apple.com/us/developer/jane-doe/id999"),
            (None, None),
        )

    def test_canonical_url_ignores_slug_and_query(self):
        self.assertEqual(
            canonical_url("https://apps.apple.com/us/app/quiet-focus/id1001?uo=4", "ios", "1001"),
            "https://apps.apple.com/us/app/id1001",
        )
        self.assertEqual(
            canonical_url("https://play.google.com/store/apps/details?id=com.example.app&hl=en", "android", "com.example.app"),
            "https://play.google.com/store/apps/details?id=com.example.app",
        )

    def test_id_matches_across_country_urls(self):
        index = DedupIndex()
        index.add("ios", "1001", "https://apps.apple.com/fr/app/id1001")
        self.assertTrue(index.contains("ios", "1001", "https://apps.apple.com/us/app/other/id1001"))
        self.assertFalse(index.contains("android", "1001", ""))

    def test_index_url_drops_country_query_and_trailing_slash(self):
        self.assertEqual(
            index_url("https://apps.apple.com/fr/app/quiet-focus/id1001?uo=4"),
            "https://apps.apple.com/app/id1001",
        )
        self.assertEqual(
            index_url("https://apps.apple.com/us/app/quiet-focus/id1001/"),
            "https://apps.apple.com/app/id1001",
        )
        index = DedupIndex()
        index.add(None, None, "https://apps.apple.com/fr/app/quiet-focus/id1001?uo=4", "Quiet Focus", "Jane Doe")
        self.assertTrue(index.contains("ios", "1001", "https://apps.apple.com/us/app/other/id1001"))
        self.assertTrue(index.contains("ios", "9999", "", "Quiet Focus", "Jane Doe"))
        self.assertFalse(index.contains("ios", "9999", "", "Quiet Focus", ""))

    def test_blank_sheet_headers_exclude_the_generated_body(self):
        sheet = _FakeSheet()
        headers = install_blank_headers(sheet)
        self.assertEqual(headers, BLANK_HEADERS)
        self.assertNotIn("Generated Body", headers)
        self.assertNotIn("Email Subject", headers)
        self.assertNotIn("Notes", headers)
        self.assertNotIn("App Store ID", headers)
        self.assertNotIn("Review Count", headers)
        self.assertIn("Contact", headers)
        self.assertIn("Contact Email", headers)
        self.assertEqual(len(headers), 13)
        self.assertIn("Estimated MRR", headers)
        self.assertIn("Buy Signal", headers)
        self.assertIn("Last contacted", headers)
        self.assertNotIn("Follow-Up Date", headers)
        self.assertEqual(sheet.updated, BLANK_HEADERS)
        self.assertEqual(sheet.frozen, 1)
        self.assertIn("bold", sheet.formatted[1]["textFormat"])
        self.assertTrue(sheet.formatted[1]["textFormat"]["bold"])

    def test_contact_email_column_is_filled(self):
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="Quiet",
            emails=[EmailFound("jane@example.com", "founder", "https://example.com", "")],
        )
        row = build_import_row(lead, ["Contact Email", "Target Email"], None, set())
        self.assertEqual(row, ["jane@example.com", "jane@example.com"])

    def test_session_duplicate_is_remembered(self):
        index = DedupIndex()
        lead = Lead(platform="ios", app_store_id="5", app_store_url="https://apps.apple.com/us/app/id5")
        self.assertFalse(index.contains_lead(lead))
        index.add_lead(lead)
        self.assertTrue(index.contains_lead(lead))


class CrmShapeTests(unittest.TestCase):
    def test_header_detection_and_migration_keep_contacts(self):
        from modules.sheets_crm import CRM_COLUMNS, migrate_row, migrate_sheet_values, schema_is_current

        values = [["Notes"], ["Appname", "Appstore URL"]]
        self.assertEqual(find_header_row(values), 1)
        legacy = [
            "App Name",
            "App Store URL",
            "Contact",
            "Contact Email",
            "Status",
            "Follow-Up Date",
            "Last Contacted",
            "Lead Score",
            "Estimated MRR",
            "Ratings",
            "Category",
            "Platform",
            "Developer Website",
        ]
        row = [
            "Tideform",
            "https://apps.apple.com/id1",
            "Maya",
            "maya@tideform.app",
            "Follow-Up 1",
            "",
            "2026-01-02",
            "74",
            "<$5K",
            "186",
            "Health & Fitness",
            "iOS",
            "https://tideform.app",
        ]
        migrated = migrate_row(legacy, row)
        self.assertEqual(migrated[CRM_COLUMNS.index("Appname")], "Tideform")
        self.assertEqual(migrated[CRM_COLUMNS.index("Buy Signal")], "")
        self.assertEqual(migrated[CRM_COLUMNS.index("Appstore URL")], "https://apps.apple.com/id1")
        self.assertEqual(migrated[CRM_COLUMNS.index("Contact")], "Maya")
        self.assertEqual(migrated[CRM_COLUMNS.index("Contact Email")], "maya@tideform.app")
        self.assertEqual(migrated[CRM_COLUMNS.index("Rating count")], "186")
        self.assertEqual(migrated[CRM_COLUMNS.index("Review count")], "")
        self.assertEqual(migrated[CRM_COLUMNS.index("Status")], "Follow-Up")
        self.assertEqual(migrated[CRM_COLUMNS.index("Platform")], "iOS")
        self.assertEqual(migrated[CRM_COLUMNS.index("Website")], "https://tideform.app")
        self.assertEqual(migrated[CRM_COLUMNS.index("Estimated MRR")], "<$5K")
        self.assertEqual(migrated[CRM_COLUMNS.index("Category")], "Health & Fitness")
        self.assertEqual(migrated[CRM_COLUMNS.index("Last contacted")], "2026-01-02")
        booked = list(row)
        booked[4] = "Call Booked"
        self.assertEqual(migrate_row(legacy, booked)[CRM_COLUMNS.index("Status")], "Booked")
        kept = list(row)
        kept[4] = "Closed / Won"
        self.assertEqual(migrate_row(legacy, kept)[CRM_COLUMNS.index("Status")], "Closed / Won")
        grid = migrate_sheet_values([legacy, row, ["", ""]], 0)
        self.assertEqual(grid[0], list(CRM_COLUMNS))
        self.assertEqual(len(grid), 3)
        self.assertEqual(grid[1][0], "Tideform")
        self.assertTrue(schema_is_current(grid[0]))
        again = migrate_row(grid[0], grid[1])
        self.assertEqual(again, grid[1])

    def test_formula_injection_is_neutralized(self):
        self.assertTrue(safe_cell("=HYPERLINK(\"x\")").startswith("'"))
        self.assertEqual(safe_cell("Quiet Focus"), "Quiet Focus")

    def test_import_row_uses_existing_order_and_statuses(self):
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="=Quiet",
            developer_name="Jane Doe",
            app_store_url="https://apps.apple.com/us/app/id1001",
            last_update="2026-11-01",
            rating_count=8,
            review_count=2,
            category="Productivity",
            score_normalized=90,
            score_points=80,
            score_known_max=90,
            emails=[EmailFound("jane@example.com", "founder", "https://example.com/privacy", "founder")],
        )
        template = {"subject": "About {{App Name}}", "body": "Hi {{Contact Name}},\n\n{{App Name}} in {{Category}}."}
        row = build_import_row(lead, ["App Name", "Target Email", "Status", "Email Subject", "Ratings", "Review Count"], template, set())
        self.assertEqual(row[0], "'=Quiet")
        self.assertEqual(row[1], "jane@example.com")
        self.assertEqual(row[2], "To Contact")
        self.assertIn("Quiet", row[3])
        self.assertEqual(row[4], "8")
        self.assertEqual(row[5], "2")

        bare = Lead(platform="ios", app_store_id="2", app_name="No Mail", app_store_url="https://apps.apple.com/us/app/id2")
        missing = build_import_row(bare, ["Status", "Email Subject"], template, set())
        self.assertEqual(missing, ["Missing email", ""])

        suppressed = build_import_row(lead, ["Status", "Email Subject"], template, {"jane@example.com"})
        self.assertEqual(suppressed[0], "Suppressed")
        self.assertEqual(suppressed[1], "")

    def test_blank_contact_name_does_not_leave_a_broken_greeting(self):
        lead = Lead(platform="ios", app_store_id="1", app_name="Quiet", category="Productivity")
        self.assertEqual(render_template("Hi {{Contact Name}},", lead), "Hi,")


class _FakeSheet:
    def __init__(self):
        self.col_count = 5
        self.updated = None
        self.formatted = None
        self.frozen = None

    def add_cols(self, count):
        self.col_count += count

    def update(self, values, range_name, value_input_option):
        self.updated = values[0]

    def format(self, cell_range, cell_format):
        self.formatted = (cell_range, cell_format)

    def freeze(self, rows=0):
        self.frozen = rows


if __name__ == "__main__":
    unittest.main()
