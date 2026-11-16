"""License binding, quota responses, offline demo, and anonymous telemetry."""

import csv
import hashlib
import json
import os
import platform
import socket
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.activity import Narrator
from modules.agent_ui import AgentDashboard
from modules.auth import (
    assert_same_machine,
    credentials_path,
    machine_fingerprint,
    save_credentials,
)
from modules.enricher import (
    ScoutClient,
    activate_license,
    checkout_url,
    dotenv_value,
    local_apify_enabled,
    public_targeting,
)
from modules.errors import LicenseError
from modules.models import EmailFound, Lead
from modules.pipeline import analyze_saved
from modules.run_store import RunStore
from modules.config import validate_targeting
from modules.prompts import ScriptedPrompter
from modules.sheets_crm import CRM_COLUMNS, EXPORT_COLUMNS, lead_to_export_row
from modules.static_demo import load_demo_catalog, load_demo_leads, merge_demo_catalog, run_static_demo
from modules.telemetry import build_event, emit_event
from main import _parser, _run_licensed, main


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("APP_SCOUT_HOME")
        os.environ["APP_SCOUT_HOME"] = self._home.name

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("APP_SCOUT_HOME", None)
        else:
            os.environ["APP_SCOUT_HOME"] = self._previous
        self._home.cleanup()

    def test_fingerprint_is_the_sha256_of_node_and_getnode(self):
        expected = hashlib.sha256(f"{platform.node()}{uuid.getnode()}".encode("utf-8")).hexdigest()
        self.assertEqual(machine_fingerprint(), expected)

    def test_activate_writes_0600_and_drops_customer_email(self):
        secret_email = "founder@example.com"

        def transport(method, url, headers, payload, timeout):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/licenses/activate"))
            self.assertEqual(headers["X-License-Key"], "TEST-KEY")
            self.assertEqual(headers["X-Machine-Fingerprint"], payload["machine_fingerprint"])
            self.assertIn("X-Instance-ID", headers)
            self.assertEqual(payload["instance_name"], payload["machine_fingerprint"])
            return _Response(
                200,
                {
                    "activated": True,
                    "instance": {"id": "inst_123"},
                    "meta": {"customer_email": secret_email},
                },
            )

        saved = activate_license("TEST-KEY", transport=transport)
        path = credentials_path()
        self.assertEqual(saved["instance_id"], "inst_123")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(on_disk), {"license_key", "instance_id", "machine_fingerprint"})
        self.assertNotIn(secret_email, path.read_text(encoding="utf-8"))

    def test_copied_credentials_fail_before_any_request(self):
        save_credentials(
            {
                "license_key": "TEST-KEY",
                "instance_id": "inst_123",
                "machine_fingerprint": "a" * 64,
            }
        )

        def transport(*_args):
            raise AssertionError("copied credentials must not call the API")

        client = ScoutClient(
            {
                "license_key": "TEST-KEY",
                "instance_id": "inst_123",
                "machine_fingerprint": "a" * 64,
            },
            transport=transport,
        )
        with self.assertRaises(LicenseError) as caught:
            client.scan({"categories": ["Productivity"]})
        self.assertIn("does not match this machine", str(caught.exception))
        with self.assertRaises(LicenseError):
            assert_same_machine(
                {
                    "license_key": "TEST-KEY",
                    "instance_id": "inst_123",
                    "machine_fingerprint": "b" * 64,
                }
            )

    def test_status_messages_for_quota_lock_and_activation_limit(self):
        credentials = {
            "license_key": "TEST-KEY",
            "instance_id": "inst_123",
            "machine_fingerprint": machine_fingerprint(),
        }

        def client_for(status):
            return ScoutClient(credentials, transport=lambda *_args: _Response(status, {"error": "TEST-KEY"}))

        with self.assertRaises(LicenseError) as quota:
            client_for(402).fetch_mrr([{"app_store_id": "1"}])
        self.assertEqual(str(quota.exception), "Monthly quota reached — upgrade or wait for cycle reset")
        self.assertEqual(quota.exception.status, 402)
        self.assertNotIn("TEST-KEY", str(quota.exception))

        with self.assertRaises(LicenseError) as locked:
            client_for(409).scan({"categories": ["Productivity"], "consider_mrr": True})
        self.assertEqual(str(locked.exception), "Another scan is currently running on this license key")
        self.assertEqual(locked.exception.status, 409)

        with self.assertRaises(LicenseError) as again:
            client_for(429).fetch_mrr([])
        self.assertEqual(str(again.exception), "Another scan is currently running on this license key")

        with self.assertRaises(LicenseError) as forbidden:
            client_for(403).scan({})
        self.assertEqual(str(forbidden.exception), "License already active on another machine")

        with self.assertRaises(LicenseError) as invalid:
            client_for(401).scan({})
        self.assertEqual(str(invalid.exception), "Invalid license key.")

    def test_scan_sends_the_three_headers_and_not_the_key_in_the_body(self):
        seen = {}

        def transport(method, url, headers, payload, timeout):
            seen["headers"] = headers
            seen["payload"] = payload
            seen["url"] = url
            return _Response(200, {"leads": []})

        credentials = {
            "license_key": "TEST-KEY",
            "instance_id": "inst_123",
            "machine_fingerprint": machine_fingerprint(),
        }
        ScoutClient(credentials, transport=transport).scan(
            {"categories": ["Health & Fitness"], "consider_mrr": False, "license_key": "TEST-KEY"}
        )
        self.assertTrue(seen["url"].endswith("/scans"))
        self.assertEqual(seen["headers"]["X-License-Key"], "TEST-KEY")
        self.assertEqual(seen["headers"]["X-Instance-ID"], "inst_123")
        self.assertEqual(seen["headers"]["X-Machine-Fingerprint"], machine_fingerprint())
        self.assertNotIn("license_key", seen["payload"])
        self.assertNotIn("license_key", seen["payload"]["targeting"])
        self.assertEqual(public_targeting({"categories": ["Finance"], "license_key": "nope"}), {"categories": ["Finance"]})

    def test_dotenv_and_environment_unlock_local_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('APIFY_TOKEN="creator-token"\n# comment\nOTHER=1\n', encoding="utf-8")
            self.assertEqual(dotenv_value("APIFY_TOKEN", path), "creator-token")
        with patch.dict(os.environ, {"APIFY_TOKEN": "from-env"}):
            self.assertTrue(local_apify_enabled())
        with patch.dict(os.environ, {"APIFY_TOKEN": ""}):
            with patch("modules.enricher.dotenv_value", return_value=""):
                with patch("modules.enricher.project_path", return_value=Path("/tmp/no-such-apify-token")):
                    self.assertFalse(local_apify_enabled())


class TelemetryTests(unittest.TestCase):
    def test_event_is_anonymous_and_non_blocking(self):
        seen = {}

        def poster(payload, timeout):
            seen["payload"] = payload
            seen["timeout"] = timeout

        thread = emit_event(
            "scan_started",
            user_profile="angel_vc",
            consider_mrr=True,
            categories=["Health & Fitness", "Productivity"],
            is_licensed=True,
            poster=poster,
        )
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        thread.join(1)
        self.assertEqual(seen["timeout"], 2.0)
        self.assertEqual(
            set(seen["payload"]),
            {"event", "user_profile", "consider_mrr", "categories", "is_licensed", "cli_version"},
        )
        encoded = json.dumps(seen["payload"])
        self.assertNotIn("license_key", encoded)
        self.assertNotIn("@", encoded)
        self.assertEqual(seen["payload"]["user_profile"], "angel_vc")
        self.assertTrue(seen["payload"]["consider_mrr"])

    def test_do_not_track_and_flag_skip_the_post(self):
        def poster(*_args):
            raise AssertionError("telemetry sent")

        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertIsNone(emit_event("scan_started", poster=poster))
        os.environ.pop("DO_NOT_TRACK", None)
        self.assertIsNone(emit_event("scan_started", no_telemetry=True, poster=poster))
        payload = build_event("demo", user_profile="other", consider_mrr=False, categories=[], is_licensed=False)
        self.assertNotIn("email", payload)


class DemoTests(unittest.TestCase):
    def test_replay_lasts_about_four_seconds_and_writes_the_export_csv(self):
        leads = load_demo_leads()
        self.assertEqual(len(leads), 10)
        self.assertEqual(list(leads[0]), list(EXPORT_COLUMNS))
        self.assertEqual(leads[0]["Appname"], "Tideform")
        fitness = load_demo_leads(categories=["Health & Fitness"])
        self.assertEqual([row["Appname"] for row in fitness], ["Tideform", "Kindred Walks", "Lumen Habits"])
        self.assertTrue(all(row["Category"] == "Health & Fitness" for row in fitness))
        self.assertEqual(fitness[0]["Buy Signal"], "🚀 Launched Paywall")
        self.assertEqual(fitness[1]["Buy Signal"], "🌱 Early Traction")
        self.assertEqual(fitness[2]["Buy Signal"], "📈 High-Velocity Indie")
        self.assertTrue(all("Velocity Est." in row["Estimated MRR"] for row in fitness))
        delays = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deals.csv"
            board = AgentDashboard(len(leads), demo=True, enabled=False)
            written = run_static_demo(
                leads=leads,
                duration=4.0,
                sleeper=delays.append,
                dashboard=board,
                export_path=path,
            )
            self.assertEqual(written, path)
            self.assertAlmostEqual(sum(delays), 4.0, places=6)
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], list(EXPORT_COLUMNS))
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[1][0], "Tideform")
        self.assertEqual(rows[1][1], "🚀 Launched Paywall")
        self.assertEqual(rows[1][4], "maya@tideform.app")
        self.assertIn("Velocity Est.", rows[1][10])
        self.assertEqual(board.saved, 10)
        self.assertTrue(all(phase["state"] == "complete" for phase in board.phases.values()))

    def test_demo_command_streams_the_matching_category_without_a_socket(self):
        def blocked(*_args, **_kwargs):
            raise AssertionError("demo opened a socket")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PROSPECTING_DEMO_SECONDS": "0", "DO_NOT_TRACK": "1"}):
                with patch("socket.create_connection", blocked), patch.object(socket.socket, "connect", blocked):
                    with patch("main.GoogleSheetCRM.connect", side_effect=AssertionError("sheet")):
                        with patch("sys.stdin.isatty", return_value=False):
                            with patch("modules.static_demo.deals_export_path", return_value=Path(tmp) / "deals.csv"):
                                code = main(["--demo", "--no-telemetry"])
            exported = Path(tmp) / "deals.csv"
            self.assertEqual(code, 0)
            self.assertTrue(exported.exists())
            with exported.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], list(EXPORT_COLUMNS))
        self.assertEqual([row[0] for row in rows[1:]], ["Tideform", "Kindred Walks", "Lumen Habits"])
        self.assertTrue(all(row[11] == "Health & Fitness" for row in rows[1:]))
        self.assertEqual(rows[1][1], "🚀 Launched Paywall")

    def test_aliases_land_on_the_nine_export_columns(self):
        row = lead_to_export_row(
            {
                "app_name": "Tideform",
                "lead_score": "",
                "Score": "74",
                "Contact": "Maya Ellison (Tideform)",
                "Contact Email": "maya@tideform.app",
                "Category": "Health & Fitness",
                "Developer Website": "https://tideform.app",
                "Ratings": "186",
                "Estimated MRR": "<$5K",
                "App Store URL": "https://apps.apple.com/us/app/tideform/id6448101001",
                "website URL": "",
            }
        )
        self.assertEqual(list(row), list(EXPORT_COLUMNS))
        self.assertEqual(row["Appname"], "Tideform")
        self.assertEqual(row["Buy Signal"], "")
        self.assertNotIn("74", row.values())
        self.assertEqual(row["Contact"], "Maya Ellison (Tideform)")
        self.assertEqual(row["Contact Email"], "maya@tideform.app")
        self.assertEqual(row["Category"], "Health & Fitness")
        self.assertEqual(row["Website"], "https://tideform.app")
        self.assertEqual(row["Rating count"], "186")
        self.assertEqual(row["Estimated MRR"], "<$5K")
        lead = Lead(
            platform="ios",
            app_store_id="6448101001",
            app_name="Tideform",
            estimated_mrr="<$5K",
            rating_count=186,
            score_normalized=74,
            contact_name="Maya Ellison (Tideform)",
            category="Health & Fitness",
            app_store_url="https://apps.apple.com/us/app/tideform/id6448101001",
            developer_website="https://tideform.app",
            emails=[EmailFound("maya@tideform.app", "founder", "https://tideform.app")],
        )
        exported = lead_to_export_row(lead)
        self.assertEqual(exported["Contact Email"], "maya@tideform.app")
        self.assertEqual(exported["Buy Signal"], "")
        self.assertNotIn("74", exported.values())

    def test_old_leads_list_groups_by_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo_leads.json"
            path.write_text(
                json.dumps(
                    {
                        "leads": [
                            {
                                "App Name": "Fieldnote",
                                "Contact": "Elias Ward",
                                "Contact Email": "elias@fieldnote.app",
                                "Category": "Education",
                                "Lead Score": "70",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = load_demo_catalog(path)
        self.assertEqual(list(catalog), ["Education"])
        self.assertEqual(catalog["Education"][0]["Appname"], "Fieldnote")

    def test_merge_keeps_cached_leads_and_caps_each_category(self):
        existing = {
            "Finance": [
                {"Appname": "Ledgerling", "Contact name": "Priya", "contact email": "priya@ledgerling.app", "App Store URL": "https://apps.apple.com/a"},
            ]
        }
        incoming = {
            "Finance": [
                {"Appname": "Ledgerling", "Contact name": "Priya", "contact email": "priya@ledgerling.app", "App Store URL": "https://apps.apple.com/a"},
                {"Appname": "Blank", "Contact name": "", "contact email": "blank@example.app", "App Store URL": "https://apps.apple.com/blank"},
                {"Appname": "New", "Contact name": "Ada", "contact email": "ada@new.app", "App Store URL": "https://apps.apple.com/new"},
            ]
        }
        merged = merge_demo_catalog(existing, incoming, limit=1)
        self.assertEqual([row["Appname"] for row in merged["Finance"]], ["Ledgerling"])
        wider = merge_demo_catalog(existing, incoming, limit=10)
        self.assertEqual([row["Appname"] for row in wider["Finance"]], ["Ledgerling", "New"])

    def test_hidden_flags_and_creator_commands(self):
        help_text = _parser().format_help()
        self.assertNotIn("--internal", help_text)
        self.assertNotIn("--seed-demo", help_text)
        self.assertIn("--demo", help_text)
        self.assertIn("--activate", help_text)
        self.assertIn("--buy", help_text)
        root = Path(__file__).resolve().parents[1]
        self.assertIn("helix", (root / ".gitignore").read_text(encoding="utf-8").splitlines())
        with patch("main.GoogleSheetCRM.connect", side_effect=AssertionError("sheet")):
            self.assertEqual(main(["check"]), 1)
            self.assertEqual(main(["--seed-demo"]), 1)
            self.assertEqual(main(["--format-sheet"]), 1)
        prompter = ScriptedPrompter(["cancel"])
        with patch("main.GoogleSheetCRM.connect", side_effect=AssertionError("sheet")):
            with patch("main.load_credentials", return_value=None):
                self.assertEqual(main([], prompter=prompter), 130)
        self.assertNotIn("min_ratings", validate_targeting({}))
        self.assertEqual(validate_targeting({"min_ratings": 30})["min_ratings"], 30)
        self.assertNotIn("min_ratings", validate_targeting({"min_ratings": "any"}))
        self.assertEqual(checkout_url(), "https://yourdomain.com/checkout")

    def test_sheet_columns_stay_thirteen(self):
        self.assertEqual(len(CRM_COLUMNS), 13)
        self.assertEqual(list(EXPORT_COLUMNS), list(CRM_COLUMNS))

    def test_buy_opens_checkout_without_a_scrape(self):
        with patch("webbrowser.open") as opened:
            with patch("main.GoogleSheetCRM.connect", side_effect=AssertionError("sheet")):
                code = main(["--buy", "--demo"])
        self.assertEqual(code, 0)
        opened.assert_called_once_with("https://yourdomain.com/checkout")


class RevenueSkipTests(unittest.TestCase):
    def test_consider_mrr_off_does_not_call_the_tracker(self):
        store = RunStore("skip-mrr")
        store.save(
            Lead(
                platform="ios",
                app_store_id="1",
                app_name="Tideform",
                estimated_mrr="$4K",
                mrr_status="pending",
            )
        )

        def getter(*_args, **_kwargs):
            raise AssertionError("tracker called")

        analyzed = analyze_saved(store, getter=getter, enabled=False, narrator=Narrator())
        self.assertEqual(analyzed[0].mrr_status, "skipped")
        self.assertEqual(analyzed[0].estimated_mrr, "$4K")

    def test_licensed_contact_run_skips_mrr_and_quota_keeps_the_leads(self):
        calls = []

        class FakeClient:
            def __init__(self, credentials):
                self.credentials = credentials

            def scan(self, targeting):
                calls.append(("scan", targeting.get("consider_mrr")))
                return {
                    "leads": [
                        {
                            "App Name": "Tideform",
                            "App Store URL": "https://apps.apple.com/us/app/tideform/id6448101001",
                            "Contact": "Maya Ellison (Tideform)",
                            "Contact Email": "maya@tideform.app",
                            "Status": "To Contact",
                            "Follow-Up Date": "",
                            "Last Contacted": "",
                            "Lead Score": "74",
                            "Estimated MRR": "",
                            "Ratings": "186",
                            "Category": "Health & Fitness",
                            "Platform": "iOS",
                            "Developer Website": "https://tideform.app",
                        }
                    ]
                }

            def fetch_mrr(self, apps):
                calls.append(("mrr", apps))
                raise LicenseError("Monthly quota reached — upgrade or wait for cycle reset", status=402)

        credentials = {"license_key": "TEST-KEY", "instance_id": "inst", "machine_fingerprint": "fp"}
        with patch("main.load_credentials", return_value=credentials), patch("main.assert_same_machine"):
            with patch("main.ScoutClient", FakeClient):
                skipped = _run_licensed(
                    {"consider_mrr": False, "template_id": "intro", "target_prospects": 1},
                    None,
                    "run",
                )
                self.assertEqual(calls, [("scan", False)])
                self.assertEqual(skipped.qualified[0].mrr_status, "skipped")
                self.assertEqual(skipped.qualified[0].primary_email.address, "maya@tideform.app")
                calls.clear()
                kept = _run_licensed(
                    {"consider_mrr": True, "template_id": "intro", "target_prospects": 1},
                    None,
                    "run",
                )
        self.assertEqual([name for name, *_rest in calls], ["scan", "mrr"])
        self.assertEqual(len(kept.qualified), 1)
        self.assertEqual(kept.qualified[0].app_name, "Tideform")


if __name__ == "__main__":
    unittest.main()
