"""Five-phase agent panel for a prospecting run.

Per-app skip lines stay off this screen. The panel is the progress UI: one
active phase, a rotating aside, an elapsed clock, and a rolling ETA.
"""

from __future__ import annotations

import sys
import time

PHASES = (
    (1, "Phase 1 — CRM Memory Sync"),
    (2, "Phase 2 — App Store Discovery"),
    (3, "Phase 3 — Contact & Revenue Enrichment"),
    (4, "Phase 4 — Lead Scoring & Ranking"),
    (5, "Phase 5 — CRM Sync & Formatting"),
)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WITTY_LINES = (
    "Bribing the App Store API with virtual cookies...",
    "Parsing developer privacy pages... (hoping they didn't use Wix)",
    "Extracting MRR from digital tea leaves...",
    "Asking Siri nicely for valid email addresses...",
    "Deduplicating leads so you don't pitch the same indie dev twice...",
    "Scanning solo founder footprints in the wild...",
)
WITTY_EVERY_SECONDS = 2.5
ETA_WARMUP_SECONDS = 3.0


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def is_high_mrr(value: str | None) -> bool:
    text = (value or "").strip()
    return bool(text) and text.upper() != "N/A"


def sheet_url(spreadsheet_id: str) -> str:
    if not spreadsheet_id:
        return ""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


class AgentDashboard:
    """State for the phase stepper. `Live` is used only on an interactive terminal."""

    def __init__(self, target: int, sheet: str = "", *, demo: bool = False, enabled: bool | None = None):
        self.target = max(0, int(target))
        self.sheet = sheet
        self.demo = demo
        self.enabled = _live_enabled() if enabled is None else enabled
        self.started_at = time.monotonic()
        self.funnel = {
            "raw_scanned": 0,
            "duplicates_skipped": 0,
            "filtered_out": 0,
            "emails_found": 0,
            "mrr_hits": 0,
            "qualified_count": 0,
        }
        self.saved = 0
        self.high_mrr = 0
        self.phases = {
            number: {
                "title": title,
                "state": "pending",
                "status": "",
                "summary": "",
                "elapsed": 0.0,
                "tick": None,
            }
            for number, title in PHASES
        }
        self._live = None
        self._console = None
        self._witty_origin = self.started_at
        self._finished = False

    def start(self) -> None:
        self.started_at = time.monotonic()
        self._witty_origin = self.started_at
        if not self.enabled or self._live is not None:
            return
        try:
            from rich.console import Console
            from rich.live import Live
        except ImportError:
            self.enabled = False
            return
        self._console = Console(highlight=False)
        self._live = Live(
            self,
            console=self._console,
            refresh_per_second=8,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start()

    def pause(self) -> None:
        """Release the terminal so a question can be asked."""
        self._stamp()
        live = self._live
        self._live = None
        if live is not None:
            live.stop()

    def resume(self) -> None:
        if not self.enabled or self._live is not None:
            self.refresh()
            return
        try:
            from rich.live import Live
        except ImportError:
            self.enabled = False
            return
        self._live = Live(
            self,
            console=self._console,
            refresh_per_second=8,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start()

    def stop(self, *, summary: bool = True) -> None:
        self._stamp()
        self.pause()
        if summary and not self._finished:
            self._finished = True
            self._print_summary()

    def focus(self, number: int, status: str = "") -> None:
        self._stamp()
        for other, phase in self.phases.items():
            if other != number and phase["state"] == "active":
                phase["state"] = "pending"
                phase["tick"] = None
        phase = self.phases[number]
        phase["state"] = "active"
        if status:
            phase["status"] = status
        phase["tick"] = time.monotonic()
        self.refresh()

    def note(self, number: int, status: str) -> None:
        phase = self.phases.get(number)
        if phase is None:
            return
        phase["status"] = status
        if phase["state"] != "active":
            self.focus(number, status)
            return
        self.refresh()

    def complete(self, number: int, summary: str) -> None:
        self._stamp()
        phase = self.phases[number]
        phase["state"] = "complete"
        phase["summary"] = summary
        phase["status"] = ""
        phase["tick"] = None
        self.refresh()

    def add_funnel(self, **counts: int) -> None:
        for key, value in counts.items():
            if key in self.funnel:
                self.funnel[key] += int(value)
        self.refresh()

    def set_qualified(self, count: int) -> None:
        self.funnel["qualified_count"] = max(0, int(count))
        self.refresh()

    def remember_save(self, leads: list) -> None:
        capped = list(leads)
        self.saved = len(capped)
        self.high_mrr = sum(1 for lead in capped if is_high_mrr(getattr(lead, "estimated_mrr", "")))

    def elapsed(self, at: float | None = None) -> float:
        moment = self.started_at if at is None else at
        if at is None:
            moment = time.monotonic()
        return max(0.0, moment - self.started_at)

    def eta_text(self, at: float | None = None) -> str:
        moment = time.monotonic() if at is None else at
        elapsed = max(0.0, moment - self.started_at)
        qualified = self.funnel["qualified_count"]
        remaining = max(0, self.target - qualified)
        if qualified <= 0 or elapsed < ETA_WARMUP_SECONDS:
            return "Calculating ETA..."
        if remaining == 0:
            return "~0s remaining"
        seconds = int(round(elapsed / qualified * remaining))
        return f"~{seconds}s remaining"

    def witty_line(self, at: float | None = None) -> str:
        moment = time.monotonic() if at is None else at
        slot = int(max(0.0, moment - self._witty_origin) / WITTY_EVERY_SECONDS)
        return WITTY_LINES[slot % len(WITTY_LINES)]

    def refresh(self) -> None:
        if self._live is None:
            return
        # Paint now. The refresh thread keeps the spinner and the witty line moving
        # between these updates, including while a network call is blocked.
        self._live.update(self, refresh=True)

    def __rich__(self):
        return self.render()

    def render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        now = time.monotonic()
        rows = []
        for number, _title in PHASES:
            rows.append(self._phase_text(number, now))
        footer = Text()
        footer.append(f"Elapsed {clock(self.elapsed(now))}", style="cyan")
        footer.append("  ·  ")
        footer.append(self.eta_text(now), style="magenta")
        qualified = self.funnel["qualified_count"]
        if self.target:
            footer.append("  ·  ")
            footer.append(f"[{qualified}/{self.target} qualified]", style="bold white")
        rows.append(Text(""))
        rows.append(footer)
        return Panel(Group(*rows), title="Prospecting agent", border_style="cyan", padding=(1, 2))

    def summary_lines(self) -> list[str]:
        scanned = self.funnel["raw_scanned"]
        unique = max(0, scanned - self.funnel["duplicates_skipped"])
        enriched = self.funnel["emails_found"]
        lines = [
            f"Total elapsed: {clock(self.elapsed())}",
            f"Funnel: Scanned {scanned} → Deduplicated {unique} → Enriched {enriched} → Exact Target Saved {self.saved}",
            f"High MRR: {self.high_mrr} lead(s) at $10k+/mo",
        ]
        if self.sheet:
            lines.append(f"Google Sheet: {self.sheet}")
        if self.demo:
            lines.append("Demo mode did not change the spreadsheet.")
        return lines

    def _phase_text(self, number: int, now: float):
        from rich.text import Text

        phase = self.phases[number]
        state = phase["state"]
        block = Text()
        if state == "complete":
            block.append("✓ ", style="green")
            block.append(phase["title"], style="green")
            if phase["summary"]:
                block.append(f"\n    {phase['summary']} in {phase['elapsed']:.1f}s", style="green")
            return block
        if state == "active":
            block.append(f"{self._spinner(now)} ", style="bold white")
            block.append(phase["title"], style="bold white")
            if phase["status"]:
                block.append(f"\n    {phase['status']}", style="white")
            if number in {2, 3}:
                block.append(f"\n    {self.witty_line(now)}", style="italic cyan")
            if self.target:
                block.append(
                    f"\n    [{self.funnel['qualified_count']}/{self.target} qualified]",
                    style="bold white",
                )
            return block
        block.append("○ ", style="dim")
        block.append(phase["title"], style="dim")
        return block

    def _spinner(self, now: float) -> str:
        return SPINNER[int(now * 8) % len(SPINNER)]

    def _stamp(self) -> None:
        now = time.monotonic()
        for phase in self.phases.values():
            tick = phase["tick"]
            if phase["state"] == "active" and tick is not None:
                phase["elapsed"] += max(0.0, now - tick)
                phase["tick"] = now

    def _print_summary(self) -> None:
        lines = self.summary_lines()
        try:
            from rich.console import Console
            from rich.panel import Panel
        except ImportError:
            print("\n".join(lines))
            return
        console = self._console or Console(highlight=False)
        body = "\n".join(lines)
        console.print(Panel(body, title="Run Summary", border_style="green", padding=(1, 2)))


def _live_enabled() -> bool:
    if not sys.stdout.isatty():
        return False
    command = " ".join(sys.argv)
    return "unittest" not in command
