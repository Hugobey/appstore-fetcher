"""Offline replay of bundled leads. This module does not touch the network."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from modules.agent_ui import AgentDashboard, is_high_mrr
from modules.config import project_path, save_json
from modules.errors import ConfigError
from modules.prompts import QuestionaryPrompter
from modules.enricher import filter_by_mrr_ranges, normalize_mrr_ranges
from modules.sheets_crm import SHEETS_IMPORT_TIP, deals_export_path, lead_to_export_row, write_export_csv


def load_demo_catalog(path: Path | None = None) -> dict[str, list[dict]]:
    """Leads keyed by category. The old {"leads": [...]} file is grouped by category."""
    path = path or project_path("data/demo_leads.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read {path.name}.") from exc
    if isinstance(raw, list):
        raw = {"leads": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} needs leads keyed by category.")
    if isinstance(raw.get("leads"), list):
        grouped: dict[str, list[dict]] = {}
        for item in raw["leads"]:
            if not isinstance(item, dict):
                raise ConfigError(f"{path.name} has a lead that is not an object.")
            row = lead_to_export_row(item)
            grouped.setdefault(row["Category"] or "Other", []).append(row)
        return grouped
    grouped = {}
    for name, items in raw.items():
        if not isinstance(items, list):
            continue
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise ConfigError(f"{path.name} has a lead that is not an object.")
            row = lead_to_export_row(item)
            row["Category"] = row["Category"] or str(name)
            rows.append(row)
        grouped[str(name)] = rows
    if not grouped:
        raise ConfigError(f"{path.name} needs leads keyed by category.")
    return grouped


def demo_mrr_ranges() -> list[str]:
    """`PROSPECTING_DEMO_MRR` is a comma-separated bucket list. Blank means no filter."""
    raw = os.environ.get("PROSPECTING_DEMO_MRR", "").strip()
    if not raw:
        return []
    return normalize_mrr_ranges([part.strip() for part in raw.split(",") if part.strip()])


def apply_demo_mrr_filter(leads: list[dict]) -> list[dict]:
    ranges = demo_mrr_ranges()
    if not ranges:
        return list(leads)
    return filter_by_mrr_ranges(leads, ranges)


def load_demo_leads(path: Path | None = None, categories: list[str] | None = None) -> list[dict]:
    catalog = load_demo_catalog(path)
    if not categories:
        rows: list[dict] = []
        for items in catalog.values():
            rows.extend(items)
        return rows
    selected: list[dict] = []
    for name in categories:
        selected.extend(catalog.get(name, [])[:10])
    return selected


def choose_demo_categories(prompter, catalog: dict[str, list[dict]]) -> list[str]:
    """Ask on a TTY. A non-interactive run uses Health & Fitness when that cache has leads."""
    populated = [name for name, rows in catalog.items() if rows]
    if not populated:
        raise ConfigError("data/demo_leads.json has no leads.")
    override = os.environ.get("PROSPECTING_DEMO_CATEGORY", "").strip()
    if override:
        return [part.strip() for part in override.split(",") if part.strip()]
    if prompter is not None and not isinstance(prompter, QuestionaryPrompter):
        return _ask_demo_categories(prompter, populated)
    if sys.stdin is None or not sys.stdin.isatty():
        return [_default_demo_category(populated)]
    if prompter is None:
        return [_default_demo_category(populated)]
    return _ask_demo_categories(prompter, populated)


def merge_demo_catalog(existing: dict, incoming: dict, limit: int = 10) -> dict:
    """Keep cached leads, then add verified newcomers. Dedupe on URL or email. Cap each category."""
    merged: dict[str, list[dict]] = {}
    names = list(existing) + [name for name in incoming if name not in existing]
    for name in names:
        kept: list[dict] = []
        seen: set[str] = set()
        for source in list(existing.get(name) or []) + list(incoming.get(name) or []):
            if not isinstance(source, dict):
                continue
            row = lead_to_export_row(source)
            row["Category"] = str(name)
            if not row["Contact"] or not row["Contact Email"]:
                continue
            key = (row.get("App Store URL") or row.get("Appstore URL") or "").strip().lower() or row["Contact Email"].strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(row)
            if len(kept) >= limit:
                break
        if kept:
            merged[name] = kept
    return merged


def export_demo_csv(leads: list[dict], path: Path) -> Path:
    return write_export_csv(leads, path)


def run_static_demo(
    *,
    export_path: Path | None = None,
    duration: float = 4.0,
    sleeper=time.sleep,
    dashboard: AgentDashboard | None = None,
    leads: list[dict] | None = None,
) -> Path:
    """Stream bundled leads through the agent stepper and write the CRM CSV.

    `duration` is the whole replay. Nothing in this function opens a socket.
    """
    leads = apply_demo_mrr_filter(list(leads) if leads is not None else load_demo_leads())
    total = len(leads)
    board = dashboard or AgentDashboard(total, demo=True)
    board.target = total
    board.demo = True
    destination = export_path or deals_export_path()
    beat, per_lead = _paces(total, duration)
    if board.enabled:
        board.start()
    board.begin_clock()

    board.focus(1, "Indexing existing CRM leads...")
    _wait(sleeper, beat)
    board.complete(1, "Indexed 0 leads")

    board.focus(2, "Scanning category feeds")
    board.set_scout("Scanning category feeds")
    _wait(sleeper, beat)
    board.add_funnel(raw_scanned=total, candidates=total)
    board.complete(2, f"Scanned {total} apps → {total} candidates")

    board.focus(3, "Extracting contacts")
    for index, lead in enumerate(leads, start=1):
        name = str(lead.get("Appname") or lead.get("App Name") or "App")
        email = str(lead.get("Contact Email") or lead.get("contact email") or "no public email")
        board.set_scout(f"Reading {name}")
        board.set_qualified(index)
        board.add_funnel(emails_found=1)
        board.push_feed(f"• {name}: {email}")
        board.note(3, f"Extracting contacts [{index}/{total} qualified]")
        if not board.enabled:
            print(board.agent_line())
        _wait(sleeper, per_lead)
    board.complete(3, f"Extracting contacts [{total}/{total} qualified]")
    board.set_scout("Scouting complete")

    board.focus(4, "Analyzing revenue")
    board.set_analysis(0, total)
    board.set_analyst(f"Analyzing revenue [0/{total}]")
    _wait(sleeper, beat)
    board.set_analysis(total, total)
    board.set_analyst(f"Analysis complete [{total}/{total}]")
    board.complete(4, f"Analyzed {total} leads")

    board.focus(5, "Writing the demo export...")
    path = export_demo_csv(leads, destination)
    _wait(sleeper, beat)
    board.remember_save([_Mrr(str(lead.get("Estimated MRR") or "")) for lead in leads])
    high = sum(1 for lead in leads if is_high_mrr(str(lead.get("Estimated MRR") or "")))
    board.high_mrr = high
    board.saved = total
    board.complete(5, f"Wrote {total} leads to {path.name}")
    board.stop(summary=True)
    return path


def seed_demo_cache() -> int:
    """Scrape up to 10 verified leads per industry and merge them into the demo cache.

    This uses the existing discovery pipeline. It does not write the Google Sheet
    and it does not call App Store Tracker.
    """
    from modules.category_feed import category_searcher
    from modules.config import load_scoring, load_settings, validate_targeting
    from modules.deduplication import DedupIndex
    from modules.enrichment import enrich_lead
    from modules.errors import ProspectingError
    from modules.labels import CATEGORIES
    from modules.pipeline import discover

    settings = load_settings()
    scoring = load_scoring(settings)
    path = project_path("data/demo_leads.json")
    catalog = load_demo_catalog(path)
    searcher = category_searcher(settings)
    incoming: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        if category == "Other":
            continue
        targeting = validate_targeting(
            {
                "categories": [category],
                "target_prospects": 10,
                "max_ratings": "any",
                "max_app_age": "any",
                "last_update": "any",
                "consider_mrr": False,
                "min_score": 0,
                "platforms": "ios",
                "country": "global",
                "developer_types": ["solo", "startup", "agency"],
                "developer_type": "solo",
                "confirmed": True,
            }
        )
        print(f"Seeding {category}...")
        try:
            discovery = discover(
                targeting,
                settings,
                scoring,
                DedupIndex(),
                searcher=searcher,
                enricher=lambda lead, settings=settings: enrich_lead(lead, settings),
                demo=False,
            )
        except ProspectingError as exc:
            print(f"{category}: {exc}")
            continue
        rows = []
        for lead in discovery.qualified:
            row = lead_to_export_row(lead)
            row["Category"] = category
            if row["Contact"] and row["Contact Email"]:
                rows.append(row)
        incoming[category] = rows[:10]
        print(f"{category}: {len(incoming[category])} verified leads.")
    merged = merge_demo_catalog(catalog, incoming, limit=10)
    save_json(path, merged)
    total = sum(len(items) for items in merged.values())
    print(f"Saved {total} demo leads to {path}.")
    print(SHEETS_IMPORT_TIP)
    return 0


def _ask_demo_categories(prompter, populated: list[str]) -> list[str]:
    selected = prompter.checkbox(
        "📂 Which App Store categories should we search? You can select more than one.",
        [(name, name) for name in populated],
        default=["Health & Fitness"] if "Health & Fitness" in populated else [populated[0]],
    )
    if selected is None:
        raise ConfigError("Select at least one category.")
    if isinstance(selected, str):
        selected = [selected]
    chosen = [item for item in selected if item]
    if not chosen:
        raise ConfigError("Select at least one category.")
    return chosen


def _default_demo_category(populated: list[str]) -> str:
    if "Health & Fitness" in populated:
        return "Health & Fitness"
    return populated[0]


def _paces(count: int, duration: float) -> tuple[float, float]:
    if duration <= 0 or count <= 0:
        return 0.0, 0.0
    beat = duration / 16.0
    per_lead = (duration - (4 * beat)) / count
    return beat, per_lead


def _wait(sleeper, seconds: float) -> None:
    if seconds > 0:
        sleeper(seconds)


class _Mrr:
    def __init__(self, estimated_mrr: str):
        self.estimated_mrr = estimated_mrr
