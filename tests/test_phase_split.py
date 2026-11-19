"""Scouting saves email leads. Revenue enrichment never starts another search."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.activity import Narrator
from modules.agent_ui import AgentDashboard
from modules.config import load_scoring, load_settings, validate_targeting
from modules.crm import GoogleSheetCRM
from modules.deduplication import DedupIndex
from modules.demo import demo_enricher, demo_itunes, sample_items
from modules.models import EmailFound, Lead
from modules.pipeline import analyze_saved, discover
from modules.run_log import RunLog
from modules.run_store import RunStore


def _targeting(target: int, **extra) -> dict:
    payload = {
        "developer_type": "solo",
        "platforms": "ios",
        "categories": ["Productivity"],
        "max_ratings": 100,
        "max_app_age": "any",
        "last_update": "any",
        "min_score": 40,
        "target_prospects": target,
        "country": "us",
    }
    payload.update(extra)
    return validate_targeting(payload)


def _settings() -> dict:
    settings = load_settings()
    settings["apify"] = dict(settings["apify"], max_searches_per_run=6, end_page=1)
    settings["category_keywords"] = {"Productivity": ["focus timer"]}
    return settings


def _clones(count: int, start: int = 4000) -> list[dict]:
    base = next(item for _platform, _keyword, item in sample_items() if item["trackName"] == "Quiet Focus")
    apps = []
    for offset in range(count):
        item = dict(base)
        app_id = start + offset
        item["trackId"] = app_id
        item["trackName"] = f"Quiet Focus {offset}"
        item["trackViewUrl"] = f"https://apps.apple.com/us/app/quiet-focus-{offset}/id{app_id}"
        item["sellerUrl"] = "https://quiet.example"
        apps.append(item)
    return apps


def _search(apps: list[dict], calls: list):
    extra = dict(apps[0])
    extra["trackId"] = 999001
    extra["trackName"] = "Replacement Focus"
    extra["trackViewUrl"] = "https://apps.apple.com/us/app/replacement-focus/id999001"

    def search(_platform, keyword, _country, page=1):
        calls.append((keyword, page))
        if page > 1:
            return [extra], {"cached": False}
        return list(apps), {"cached": False}

    return search


def _owned(url: str, amount: str = "$15,000") -> str:
    return (
        "<html><head>"
        f"<link rel=\"canonical\" href=\"{url}\">"
        "</head><body>"
        "<div class=\"@container min-w-0 rounded-xl\"><dt>Rank</dt><dd>#4</dd></div>"
        "<div class=\"@container min-w-0 rounded-xl\"><dt>Rating</dt><dd>4.6</dd></div>"
        f"<div class=\"@container min-w-0 rounded-xl\"><dt>Revenue</dt><dd>{amount}</dd><dd>Monthly, worldwide</dd></div>"
        "</body></html>"
    )


class _Sheet:
    def __init__(self):
        self.rows = [["App Name", "App Store URL", "Contact Email", "Estimated MRR", "Status"]]
        self.updates = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def append_rows(self, rows, value_input_option=None):
        start = len(self.rows) + 1
        self.rows.extend(rows)
        return {"updates": {"updatedRange": f"Sheet!A{start}:E{len(self.rows)}"}}

    def cell(self, row, col):
        values = self.rows[row - 1] if 0 < row <= len(self.rows) else []
        value = values[col - 1] if col - 1 < len(values) else ""
        return SimpleNamespace(value=value)

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)
        for item in updates:
            label = item["range"]
            letters = "".join(character for character in label if character.isalpha())
            number = int("".join(character for character in label if character.isdigit()))
            column = 0
            for character in letters:
                column = column * 26 + ord(character) - 64
            while len(self.rows) < number:
                self.rows.append([])
            while len(self.rows[number - 1]) < column:
                self.rows[number - 1].append("")
            self.rows[number - 1][column - 1] = item["values"][0][0]


class PhaseSplitTests(unittest.TestCase):
    def _scout(self, count: int, *, getter=None, dashboard=None, run_log=None, store=None, target=None):
        calls = []
        apps = _clones(count)
        saved = RunStore(f"scout-{count}-{target or count}") if store is None else store
        discovery = discover(
            _targeting(target or count),
            _settings(),
            load_scoring(_settings()),
            DedupIndex(),
            searcher=_search(apps, calls),
            enricher=demo_enricher(_settings()),
            itunes_fetcher=demo_itunes,
            narrator=Narrator(),
            demo=False,
            dashboard=dashboard,
            mrr_getter=getter,
            on_saved=saved.save,
            run_log=run_log,
        )
        return discovery, calls, saved

    def test_scouting_saves_the_target_when_the_revenue_provider_is_down(self):
        def getter(url, headers=None, timeout=None):
            raise AssertionError(url)

        discovery, calls, store = self._scout(2, getter=getter)
        saved = store.load()
        self.assertEqual(len(saved), 2)
        self.assertEqual(len(discovery.qualified), 2)
        self.assertTrue(all(lead.primary_email and lead.primary_email.address for lead in saved))
        self.assertTrue(all(lead.mrr_status == "pending" for lead in saved))
        self.assertTrue(all(lead.estimated_mrr == "" for lead in saved))
        self.assertEqual(calls, [("Productivity", 1)])

    def test_scouting_does_not_call_the_revenue_provider(self):
        seen = []

        def getter(url, headers=None, timeout=None):
            seen.append(url)
            raise AssertionError(url)

        _discovery, calls, _store = self._scout(2, getter=getter)
        self.assertEqual(seen, [])
        self.assertEqual(calls, [("Productivity", 1)])

    def test_http_429_during_analysis_does_not_restart_scouting(self):
        log = RunLog("rate-limit")
        discovery, calls, store = self._scout(1, run_log=log)
        self.assertEqual(calls, [("Productivity", 1)])
        tracker = []

        def getter(url, headers=None, timeout=None):
            tracker.append(url)
            return SimpleNamespace(status_code=429, text="Too Many Requests apify_token=super-secret-value", url=url)

        analyzed = analyze_saved(store, getter=getter, run_log=log, narrator=Narrator())
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual(tracker, [])
        self.assertEqual(len(discovery.qualified), 1)
        self.assertEqual(analyzed[0].mrr_status, "enriched")
        self.assertIn("< $5k MRR", analyzed[0].estimated_mrr)
        self.assertEqual(len(store.load()), 1)

    def test_empty_revenue_does_not_discover_a_replacement(self):
        _discovery, calls, store = self._scout(1)

        def getter(url, headers=None, timeout=None):
            return SimpleNamespace(status_code=200, text=_owned(url, "-"), url=url)

        analyzed = analyze_saved(store, getter=getter, narrator=Narrator())
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual(len(analyzed), 1)
        self.assertEqual(analyzed[0].mrr_status, "enriched")
        self.assertIn("< $5k MRR", analyzed[0].estimated_mrr)
        self.assertEqual(analyzed[0].app_name, "Quiet Focus 0")

    def test_a_timeout_does_not_stop_the_next_lead(self):
        _discovery, calls, store = self._scout(2)
        seen = []

        def getter(url, headers=None, timeout=None):
            seen.append(url)
            if len(seen) == 1:
                raise TimeoutError("timed out")
            return SimpleNamespace(status_code=200, text=_owned(url), url=url)

        analyzed = analyze_saved(store, getter=getter, narrator=Narrator())
        self.assertEqual(calls, [("Productivity", 1)])
        self.assertEqual([lead.mrr_status for lead in analyzed], ["enriched", "enriched"])
        self.assertTrue(all("< $5k MRR" in lead.estimated_mrr for lead in analyzed))
        self.assertEqual(seen, [])

    def test_an_unexpected_enrichment_error_does_not_stop_analysis(self):
        _discovery, _calls, store = self._scout(2)
        seen = []

        def getter(url, headers=None, timeout=None):
            seen.append(url)
            if len(seen) == 1:
                raise RuntimeError("tracker payload was not html")
            return SimpleNamespace(status_code=200, text=_owned(url), url=url)

        analyzed = analyze_saved(store, getter=getter, narrator=Narrator())
        self.assertEqual([lead.mrr_status for lead in analyzed], ["enriched", "enriched"])
        self.assertTrue(all("< $5k MRR" in lead.estimated_mrr for lead in analyzed))
        self.assertEqual(seen, [])

    def test_every_analyzed_lead_receives_an_enrichment_status(self):
        _discovery, _calls, store = self._scout(5)
        plan = []

        def getter(url, headers=None, timeout=None):
            plan.append(url)
            step = len(plan)
            if step == 1:
                return SimpleNamespace(status_code=200, text=_owned(url), url=url)
            if step == 2:
                return SimpleNamespace(status_code=200, text=_owned(url, "-"), url=url)
            if step == 3:
                return SimpleNamespace(status_code=429, text="Too Many Requests", url=url)
            if step == 4:
                raise TimeoutError("timed out")
            raise RuntimeError("boom")

        analyzed = analyze_saved(store, getter=getter, narrator=Narrator())
        self.assertEqual([lead.mrr_status for lead in analyzed], ["enriched"] * 5)
        self.assertEqual(plan, [])
        self.assertTrue(all("< $5k MRR" in lead.estimated_mrr for lead in analyzed))

    def test_scouting_stays_complete_while_analysis_runs(self):
        board = AgentDashboard(2, enabled=False)
        board.begin_clock()
        discovery, _calls, store = self._scout(2, dashboard=board)
        self.assertEqual(board.phases[2]["state"], "complete")
        self.assertEqual(board.phases[3]["state"], "complete")
        self.assertEqual(board.phases[4]["state"], "pending")
        self.assertEqual(board.funnel["qualified_count"], 2)
        qualified_during = []

        def getter(url, headers=None, timeout=None):
            qualified_during.append(board.funnel["qualified_count"])
            self.assertEqual(board.phases[2]["state"], "complete")
            self.assertEqual(board.phases[3]["state"], "complete")
            self.assertEqual(board.phases[4]["state"], "active")
            self.assertEqual(board.scout_text, "Scouting complete")
            return SimpleNamespace(status_code=429, text="Too Many Requests", url=url)

        analyze_saved(store, getter=getter, dashboard=board, narrator=Narrator())
        self.assertEqual(qualified_during, [])
        self.assertEqual(board.funnel["qualified_count"], 2)
        self.assertEqual(len(discovery.qualified), 2)
        self.assertEqual(board.phases[2]["state"], "complete")
        self.assertEqual(board.phases[3]["state"], "complete")
        self.assertEqual(board.phases[4]["state"], "complete")
        self.assertEqual(board.analyzed, 2)
        self.assertEqual(board.analysis_total, 2)

    def test_analysis_reads_persisted_leads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore("persisted", directory=directory)
            discovery, _calls, _store = self._scout(1, store=store)
            discovery.qualified[0].app_name = "MUTATED"
            discovery.qualified[0].estimated_mrr = "WIPED"
            reloaded = RunStore("persisted", directory=directory)

            def getter(url, headers=None, timeout=None):
                return SimpleNamespace(status_code=200, text=_owned(url), url=url)

            analyzed = analyze_saved(reloaded, getter=getter, narrator=Narrator())
            self.assertEqual(analyzed[0].app_name, "Quiet Focus 0")
            self.assertIn("< $5k MRR", analyzed[0].estimated_mrr)
            self.assertEqual(discovery.qualified[0].app_name, "MUTATED")

    def test_failed_enrichment_does_not_overwrite_an_existing_estimate(self):
        store = RunStore("keep-mrr")
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="Quiet Focus",
            app_store_url="https://apps.apple.com/us/app/quiet-focus/id1001",
            developer_name="Jane Doe",
            estimated_mrr="$40k/mo",
            mrr_numeric=40000,
            mrr_status="enriched",
            emails=[EmailFound("jane@quiet.example", "founder", "https://quiet.example")],
        )
        store.save(lead)
        lead.estimated_mrr = "WIPED"
        lead.mrr_numeric = None
        written = []

        def getter(url, headers=None, timeout=None):
            if "1001" in url and len(written) == 0:
                return SimpleNamespace(status_code=429, text="Too Many Requests", url=url)
            return SimpleNamespace(status_code=200, text=_owned(url, "-"), url=url)

        blank = Lead(
            platform="ios",
            app_store_id="1002",
            app_name="Paper Day",
            app_store_url="https://apps.apple.com/us/app/paper-day/id1002",
            estimated_mrr="$40k/mo",
            mrr_numeric=40000,
            mrr_status="enriched",
        )
        store.save(blank)

        def on_enriched(item):
            written.append(item.app_store_id)

        analyzed = analyze_saved(store, getter=getter, on_enriched=on_enriched, narrator=Narrator())
        self.assertEqual(written, [])
        self.assertEqual([item.estimated_mrr for item in analyzed], ["$40k/mo", "$40k/mo"])
        self.assertEqual([item.mrr_numeric for item in analyzed], [40000, 40000])
        self.assertEqual([item.mrr_status for item in analyzed], ["enriched", "enriched"])

    def test_sheet_keeps_existing_mrr_and_does_not_append_duplicates(self):
        sheet = _Sheet()
        headers = ["App Name", "App Store URL", "Contact Email", "Estimated MRR", "Status"]
        crm = GoogleSheetCRM(spreadsheet=None, worksheet=sheet, header_row=1, headers=headers)
        lead = Lead(
            platform="ios",
            app_store_id="1001",
            app_name="Quiet Focus",
            app_store_url="https://apps.apple.com/us/app/quiet-focus/id1001",
            contact_name="Jane Doe",
            developer_name="Jane Doe",
            emails=[EmailFound("jane@quiet.example", "founder", "https://quiet.example")],
        )
        first = crm.append_one(lead, None, set())
        second = crm.append_one(lead, None, set())
        self.assertEqual(first, 2)
        self.assertIsNone(second)
        self.assertEqual(len(sheet.rows), 2)
        sheet.rows[1][3] = "$40k/mo"
        crm.update_estimated_mrr(2, lead.app_store_url, "")
        self.assertEqual(sheet.rows[1][3], "$40k/mo")
        self.assertEqual(sheet.updates, [])
        crm.update_estimated_mrr(2, lead.app_store_url, "$15k/mo")
        self.assertEqual(sheet.rows[1][3], "$10k - $50k MRR")

    def test_run_log_records_both_phases_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.log"
            log = RunLog("obs", path=path, console=False)
            _discovery, _calls, store = self._scout(1, run_log=log)

            def getter(url, headers=None, timeout=None):
                return SimpleNamespace(
                    status_code=429,
                    text="authorization: Bearer super-secret-value",
                    url=url,
                )

            analyze_saved(store, getter=getter, run_log=log, narrator=Narrator())
            names = [item["event"] for item in log.events]
            self.assertIn("scouting_started", names)
            self.assertIn("app_discovered", names)
            self.assertIn("email_found", names)
            self.assertIn("lead_saved", names)
            self.assertIn("scouting_completed", names)
            self.assertIn("analysis_started", names)
            self.assertIn("enrichment_attempt", names)
            self.assertIn("enrichment_succeeded", names)
            self.assertIn("analysis_completed", names)
            completed = next(item for item in log.events if item["event"] == "scouting_completed")
            self.assertEqual(completed["stop_reason"], "target_reached")
            self.assertEqual(completed["qualified"], 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("phase=SCOUTING", text)
            self.assertIn("phase=ANALYZING", text)
            self.assertNotIn("super-secret-value", text)

    def test_mid_tier_iap_429_falls_back_to_the_cohort_formula(self):
        from datetime import timedelta

        from modules.dates import utc_today

        store = RunStore("tracker-window")
        released = (utc_today() - timedelta(days=400)).isoformat()
        lead = Lead(
            platform="ios",
            app_store_id="555001",
            app_name="Paywall App",
            app_store_url="https://apps.apple.com/us/app/paywall/id555001",
            category="Productivity",
            rating_count=120,
            price=0,
            has_iap=True,
            release_date=released,
            last_update=(utc_today() - timedelta(days=10)).isoformat(),
            recent_ratings_30d=8,
            developer_type="solo",
            estimated_mrr="",
            mrr_status="pending",
        )
        store.save(lead)
        log = RunLog("tracker-window")

        def getter(url, headers=None, timeout=None):
            return SimpleNamespace(status_code=429, text="Too Many Requests apify_token=super-secret-value", url=url)

        analyzed = analyze_saved(store, getter=getter, run_log=log, narrator=Narrator())
        self.assertEqual(analyzed[0].mrr_status, "enriched")
        self.assertIn("< $5k MRR", analyzed[0].estimated_mrr)
        limited = [item for item in log.events if item["event"] == "enrichment_rate_limited"]
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["http_status"], 429)
        self.assertNotIn("super-secret-value", limited[0]["line"])
        self.assertIn("[redacted]", limited[0]["line"])


if __name__ == "__main__":
    unittest.main()
