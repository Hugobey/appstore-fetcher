import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agent_ui import AgentDashboard, WITTY_LINES, clock, is_high_mrr, sheet_url


class AgentDashboardTests(unittest.TestCase):
    def test_clock_stays_at_zero_until_scraping_starts(self):
        board = AgentDashboard(5, enabled=False)
        self.assertIsNone(board.started_at)
        self.assertEqual(board.elapsed(), 0)
        self.assertEqual(board.eta_text(), "ETA: 00:00:35")
        self.assertEqual(clock(board.elapsed()), "00:00:00")
        wide = AgentDashboard(15, enabled=False)
        self.assertEqual(wide.eta_text(), "ETA: 00:01:45")
        board.begin_clock()
        origin = board.started_at
        board.start()
        self.assertEqual(board.started_at, origin)

    def test_witty_lines_rotate_and_eta_adapts_after_the_baseline(self):
        board = AgentDashboard(5, sheet=sheet_url("sheet-id"), enabled=False)
        board.begin_clock()
        self.assertEqual(board.witty_line(board.started_at), WITTY_LINES[0])
        self.assertNotEqual(board.witty_line(board.started_at), board.witty_line(board.started_at + 2.6))
        self.assertEqual(board.eta_text(board.started_at + 1), "ETA: 00:00:35")
        self.assertEqual(board.eta_text(board.started_at + 10), "ETA: 00:00:35")
        board.set_qualified(2)
        self.assertEqual(board.eta_text(board.started_at + 10), "ETA: 00:00:15")
        board.set_qualified(1)
        self.assertEqual(board.eta_text(board.started_at + 84), "ETA: 00:05:36")
        self.assertEqual(clock(75), "00:01:15")
        self.assertEqual(clock(5025), "01:23:45")

    def test_completed_phase_and_summary_card(self):
        board = AgentDashboard(5, sheet=sheet_url("abc123"), enabled=False)
        board.focus(1, "Indexing existing CRM leads...")
        board.phases[1]["elapsed"] = 0.8
        board.complete(1, "Indexed 3 leads")
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), record=True, width=220, force_terminal=True)
        console.print(board.render())
        text = console.export_text()
        self.assertIn("Phase 1 — CRM Memory Sync", text)
        self.assertIn("Indexed 3 leads in 0.8s", text)
        self.assertIn("ETA: 00:00:35", text)
        self.assertIn("Elapsed: 00:00:00", text)
        board.set_analyst("Fetching MRR for Cal AI...")
        board.push_feed("• Cal AI: No public MRR listed — keeping lead")
        console.print(board.render())
        text = console.export_text()
        self.assertIn("Scout Agent: Scanning category feeds [0/5 qualified]", text)
        self.assertIn("Fetching MRR for Cal AI...", text)
        self.assertIn("• Cal AI: No public MRR listed — keeping lead", text)
        self.assertIn("✓", text)
        board.add_funnel(raw_scanned=40, duplicates_skipped=6, emails_found=8)
        board.remember_save([_Lead("$15k/mo"), _Lead(""), _Lead("N/A")])
        self.assertFalse(is_high_mrr("<$5K"))
        lines = "\n".join(board.summary_lines())
        self.assertIn("Total time elapsed:", lines)
        self.assertIn("Scanned 40", lines)
        self.assertIn("Deduplicated 34", lines)
        self.assertIn("Enriched 8", lines)
        self.assertIn("Exact Target Saved 3", lines)
        self.assertIn("High MRR: 1 lead(s) at $10k+/mo", lines)
        self.assertIn("https://docs.google.com/spreadsheets/d/abc123", lines)
        self.assertNotIn("Top bottleneck:", lines)
        board.rejections = {"update_window": 12, "no_email_found": 3}
        short = "\n".join(board.summary_lines())
        self.assertIn("Top bottleneck: update window (12)", short)


class _Lead:
    def __init__(self, estimated_mrr: str):
        self.estimated_mrr = estimated_mrr


if __name__ == "__main__":
    unittest.main()
