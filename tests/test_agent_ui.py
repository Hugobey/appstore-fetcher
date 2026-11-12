import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agent_ui import AgentDashboard, WITTY_LINES, clock, sheet_url


class AgentDashboardTests(unittest.TestCase):
    def test_witty_lines_rotate_and_eta_waits_for_throughput(self):
        board = AgentDashboard(5, sheet=sheet_url("sheet-id"), enabled=False)
        self.assertEqual(board.witty_line(board.started_at), WITTY_LINES[0])
        self.assertNotEqual(board.witty_line(board.started_at), board.witty_line(board.started_at + 2.6))
        self.assertEqual(board.eta_text(board.started_at + 1), "Calculating ETA...")
        self.assertEqual(board.eta_text(board.started_at + 10), "Calculating ETA...")
        board.set_qualified(2)
        self.assertEqual(board.eta_text(board.started_at + 2), "Calculating ETA...")
        self.assertEqual(board.eta_text(board.started_at + 10), "~15s remaining")
        self.assertEqual(clock(75), "01:15")

    def test_completed_phase_and_summary_card(self):
        board = AgentDashboard(5, sheet=sheet_url("abc123"), enabled=False)
        board.focus(1, "Indexing existing CRM leads...")
        board.phases[1]["elapsed"] = 0.8
        board.complete(1, "Indexed 3 leads")
        from io import StringIO

        from rich.console import Console

        console = Console(file=StringIO(), record=True, width=100, force_terminal=True)
        console.print(board.render())
        text = console.export_text()
        self.assertIn("Phase 1 — CRM Memory Sync", text)
        self.assertIn("Indexed 3 leads in 0.8s", text)
        self.assertIn("✓", text)
        board.add_funnel(raw_scanned=40, duplicates_skipped=6, emails_found=8)
        board.remember_save([_Lead("$15k/mo"), _Lead(""), _Lead("N/A")])
        lines = "\n".join(board.summary_lines())
        self.assertIn("Scanned 40", lines)
        self.assertIn("Deduplicated 34", lines)
        self.assertIn("Enriched 8", lines)
        self.assertIn("Exact Target Saved 3", lines)
        self.assertIn("High MRR: 1 lead(s) at $10k+/mo", lines)
        self.assertIn("https://docs.google.com/spreadsheets/d/abc123", lines)


class _Lead:
    def __init__(self, estimated_mrr: str):
        self.estimated_mrr = estimated_mrr


if __name__ == "__main__":
    unittest.main()
