import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.crm import BLANK_HEADERS, build_import_row
from modules.models import EmailFound, Lead
from modules.mrr_enricher import enrich_estimated_mrr, fetch_mrr, parse_mrr, tracker_url, tracker_url_for_lead
from modules.sheets_crm import CRM_COLUMNS, STATUS_OPTIONS, layout_requests, rgb


class TrackerUrlTests(unittest.TestCase):
    def test_slug_and_numeric_id_drop_the_literal_id_prefix(self):
        store = "https://apps.apple.com/us/app/sleepsense-sleep-tracker/id6791916451"
        self.assertEqual(
            tracker_url(store),
            "https://www.appstoretracker.com/app/sleepsense-sleep-tracker-6791916451",
        )
        self.assertEqual(
            tracker_url("https://apps.apple.com/fr/app/sleepsense-sleep-tracker/id6791916451?uo=4"),
            "https://www.appstoretracker.com/app/sleepsense-sleep-tracker-6791916451",
        )

    def test_canonical_crm_url_recovers_the_slug(self):
        lead = Lead(
            platform="ios",
            app_store_id="6791916451",
            app_name="SleepSense: Sleep Tracker",
            app_store_url="https://apps.apple.com/us/app/id6791916451",
            source={"trackViewUrl": "https://apps.apple.com/us/app/sleepsense-sleep-tracker/id6791916451"},
        )
        self.assertEqual(
            tracker_url_for_lead(lead),
            "https://www.appstoretracker.com/app/sleepsense-sleep-tracker-6791916451",
        )
        bare = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="Quiet Focus",
            app_store_url="https://apps.apple.com/us/app/id1001",
        )
        self.assertEqual(
            tracker_url_for_lead(bare),
            "https://www.appstoretracker.com/app/quiet-focus-1001",
        )

    def test_url_without_a_slug_is_skipped(self):
        self.assertEqual(tracker_url("https://apps.apple.com/us/app/id6791916451"), "")
        self.assertEqual(
            tracker_url("https://play.google.com/store/apps/details?id=com.example.app"),
            "",
        )


class MrrParseTests(unittest.TestCase):
    def test_monthly_revenue_block_keeps_amounts_at_or_above_10k(self):
        high = """
            <div>
              <dt>Revenue</dt>
              <dd>$480K</dd>
              <dd>Monthly, worldwide</dd>
            </div>
        """
        self.assertEqual(parse_mrr(high), "$480k/mo")
        inline = "<p>Estimated MRR $15k/mo for this app.</p>"
        self.assertEqual(parse_mrr(inline), "$15k/mo")

    def test_null_and_small_revenue_stay_blank(self):
        low = "<div><dt>Revenue</dt><dd>$4K</dd><dd>Monthly</dd></div>"
        self.assertEqual(parse_mrr(low), "")
        self.assertEqual(parse_mrr("<p>$9k/mo</p>"), "")
        self.assertEqual(parse_mrr(""), "")
        self.assertEqual(parse_mrr("<html></html>"), "")


class MrrFetchTests(unittest.TestCase):
    def test_timeout_block_and_empty_page_do_not_raise(self):
        def timed_out(url, headers=None, timeout=None):
            raise TimeoutError(url)

        def blocked(url, headers=None, timeout=None):
            return SimpleNamespace(status_code=403, text="blocked")

        def empty(url, headers=None, timeout=None):
            return SimpleNamespace(status_code=200, text="<html></html>")

        sample = "https://www.appstoretracker.com/app/sleepsense-sleep-tracker-6791916451"
        self.assertEqual(fetch_mrr(sample, getter=timed_out), "N/A")
        self.assertEqual(fetch_mrr(sample, getter=blocked), "N/A")
        self.assertEqual(fetch_mrr(sample, getter=empty), "")

    def test_qualified_leads_are_fetched_concurrently(self):
        leads = [
            Lead(
                platform="ios",
                app_store_id=str(3000 + index),
                app_store_url=f"https://apps.apple.com/us/app/sample-app-{index}/id{3000 + index}",
            )
            for index in range(5)
        ]
        leads.append(
            Lead(
                platform="android",
                app_store_id="com.example.app",
                app_store_url="https://play.google.com/store/apps/details?id=com.example.app",
            )
        )
        state = {"active": 0, "max": 0, "calls": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def getter(url, headers=None, timeout=None):
            self.assertEqual(timeout, 5.0)
            self.assertIn("Mozilla", headers["User-Agent"])
            with lock:
                state["calls"] += 1
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            barrier.wait(timeout=2)
            with lock:
                state["active"] -= 1
            return SimpleNamespace(status_code=200, text="<p>$22k/mo</p>")

        enrich_estimated_mrr(leads, demo=False, getter=getter, max_workers=5)
        self.assertEqual(state["calls"], 5)
        self.assertEqual(state["max"], 5)
        self.assertTrue(all(lead.estimated_mrr == "$22k/mo" for lead in leads[:5]))
        self.assertEqual(leads[-1].estimated_mrr, "")

    def test_demo_mode_does_not_call_the_network(self):
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_store_url="https://apps.apple.com/us/app/quiet-focus/id1001",
        )
        blank = Lead(
            platform="ios",
            app_store_id="1002",
            app_store_url="https://apps.apple.com/us/app/little-habits/id1002",
        )

        def getter(url, headers=None, timeout=None):
            raise AssertionError(url)

        enrich_estimated_mrr([lead, blank], demo=True, getter=getter)
        self.assertEqual(lead.estimated_mrr, "$15k/mo")
        self.assertEqual(blank.estimated_mrr, "")


class SheetLayoutTests(unittest.TestCase):
    def test_blank_schema_excludes_email_draft_columns(self):
        self.assertEqual(list(CRM_COLUMNS), BLANK_HEADERS)
        self.assertNotIn("Generated Body", CRM_COLUMNS)
        self.assertNotIn("Email Subject", CRM_COLUMNS)
        self.assertEqual(CRM_COLUMNS[4], "Status")
        self.assertEqual(len(CRM_COLUMNS), 18)

    def test_new_row_defaults_status_and_leaves_follow_up_blank(self):
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="Quiet Focus",
            app_store_url="https://apps.apple.com/us/app/quiet-focus/id1001",
            estimated_mrr="$15k/mo",
            score_normalized=100,
            emails=[EmailFound("jane@quiet.example", "founder", "https://quiet.example", "")],
        )
        row = build_import_row(lead, list(CRM_COLUMNS), {"subject": "Hi", "body": "Hello"}, set())
        self.assertEqual(row[CRM_COLUMNS.index("Status")], "To Contact")
        self.assertEqual(row[CRM_COLUMNS.index("Follow-Up Date")], "")
        self.assertEqual(row[CRM_COLUMNS.index("Last Contacted")], "")
        self.assertEqual(row[CRM_COLUMNS.index("Estimated MRR")], "$15k/mo")
        self.assertEqual(row[CRM_COLUMNS.index("Contact Email")], "jane@quiet.example")

    def test_batch_update_payload_formats_the_canonical_sheet(self):
        requests = layout_requests(42, list(CRM_COLUMNS), header_row=1, existing_rule_count=2)
        deletes = [item["deleteConditionalFormatRule"] for item in requests if "deleteConditionalFormatRule" in item]
        self.assertEqual(
            deletes,
            [{"sheetId": 42, "index": 1}, {"sheetId": 42, "index": 0}],
        )
        validation = next(item["setDataValidation"] for item in requests if "setDataValidation" in item)
        self.assertEqual(validation["range"]["startRowIndex"], 1)
        self.assertEqual(validation["range"]["endRowIndex"], 1000)
        self.assertEqual(validation["range"]["startColumnIndex"], 4)
        self.assertEqual(validation["range"]["endColumnIndex"], 5)
        self.assertEqual(validation["rule"]["condition"]["type"], "ONE_OF_LIST")
        self.assertTrue(validation["rule"]["showCustomUi"])
        self.assertFalse(validation["rule"]["strict"])
        values = [item["userEnteredValue"] for item in validation["rule"]["condition"]["values"]]
        self.assertEqual(values, STATUS_OPTIONS)

        formulas = {}
        for item in requests:
            added = item.get("addConditionalFormatRule")
            if not added:
                continue
            span = added["rule"]["ranges"][0]
            self.assertEqual((span["startColumnIndex"], span["endColumnIndex"]), (0, 18))
            self.assertEqual((span["startRowIndex"], span["endRowIndex"]), (1, 1000))
            formula = added["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            formulas[formula] = added["rule"]["booleanRule"]["format"]
        self.assertEqual(formulas['=$E2="To Contact"']["backgroundColor"], rgb("#F8F9FA"))
        self.assertNotIn("textFormat", formulas['=$E2="To Contact"'])
        sent = formulas['=$E2="Sent"']
        self.assertEqual(sent["backgroundColor"], rgb("#E8F0FE"))
        self.assertEqual(sent["textFormat"]["foregroundColor"], rgb("#1967D2"))
        self.assertEqual(formulas['=$E2="Follow-Up 1"']["backgroundColor"], rgb("#E8F0FE"))
        self.assertEqual(formulas['=$E2="Replied"']["backgroundColor"], rgb("#FEF7E0"))
        self.assertEqual(formulas['=$E2="Replied"']["textFormat"]["foregroundColor"], rgb("#B06000"))
        self.assertEqual(formulas['=$E2="Call Booked"']["backgroundColor"], rgb("#FEF7E0"))
        won = formulas['=$E2="Closed / Won"']
        self.assertEqual(won["backgroundColor"], rgb("#E6F4EA"))
        self.assertEqual(won["textFormat"]["foregroundColor"], rgb("#137333"))
        passed = formulas['=$E2="Not Interested"']
        self.assertEqual(passed["backgroundColor"], rgb("#FCE8E6"))
        self.assertEqual(passed["textFormat"]["foregroundColor"], rgb("#80868B"))
        self.assertTrue(passed["textFormat"]["strikethrough"])

        header = next(item["repeatCell"] for item in requests if "repeatCell" in item)
        painted = header["cell"]["userEnteredFormat"]
        self.assertEqual(painted["backgroundColor"], rgb("#1E293B"))
        self.assertEqual(painted["textFormat"]["foregroundColor"], rgb("#FFFFFF"))
        self.assertTrue(painted["textFormat"]["bold"])
        self.assertEqual(painted["verticalAlignment"], "MIDDLE")
        self.assertEqual(header["range"]["startRowIndex"], 0)
        self.assertEqual(header["range"]["endRowIndex"], 1)
        frozen = next(item["updateSheetProperties"] for item in requests if "updateSheetProperties" in item)
        self.assertEqual(frozen["properties"]["gridProperties"]["frozenRowCount"], 1)
        basic = next(item["setBasicFilter"] for item in requests if "setBasicFilter" in item)
        self.assertEqual(basic["filter"]["range"]["startRowIndex"], 0)
        self.assertEqual(basic["filter"]["range"]["endRowIndex"], 1000)
        self.assertEqual(basic["filter"]["range"]["endColumnIndex"], 18)
        widths = {
            item["updateDimensionProperties"]["range"]["startIndex"]: item["updateDimensionProperties"]["properties"]["pixelSize"]
            for item in requests
            if "updateDimensionProperties" in item
        }
        self.assertGreater(widths[CRM_COLUMNS.index("App Name")], widths[CRM_COLUMNS.index("Status")])
        self.assertGreater(widths[CRM_COLUMNS.index("Notes")], widths[CRM_COLUMNS.index("Ratings")])
        self.assertGreater(widths[CRM_COLUMNS.index("App Store URL")], widths[CRM_COLUMNS.index("Lead Score")])
        self.assertGreater(widths[CRM_COLUMNS.index("Contact Email")], widths[CRM_COLUMNS.index("Estimated MRR")])

    def test_existing_status_column_keeps_its_letter(self):
        headers = [
            "App Name",
            "Developer",
            "Target Email",
            "Contact Name",
            "App Store URL",
            "Last Update Date",
            "Status",
            "Notes",
        ]
        requests = layout_requests(7, headers, header_row=1, existing_rule_count=0)
        validation = next(item["setDataValidation"] for item in requests if "setDataValidation" in item)
        self.assertEqual(validation["range"]["startColumnIndex"], 6)
        self.assertEqual(validation["range"]["endColumnIndex"], 7)
        formulas = [
            item["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
            for item in requests
            if "addConditionalFormatRule" in item
        ]
        self.assertIn('=$G2="To Contact"', formulas)
        self.assertTrue(all(item["addConditionalFormatRule"]["rule"]["ranges"][0]["endColumnIndex"] == 8 for item in requests if "addConditionalFormatRule" in item))
        self.assertFalse(any("deleteConditionalFormatRule" in item for item in requests))


if __name__ == "__main__":
    unittest.main()
