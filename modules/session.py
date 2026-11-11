"""Save retrieved apps so a stopped run can be resumed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from modules.config import save_json
from modules.models import Lead, lead_from_dict, lead_to_dict


def new_session(path: Path, targeting: dict) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {
        "version": 1,
        "id": stamp,
        "stage": "running",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "targeting": targeting,
        "completed_jobs": [],
        "qualified": [],
        "missing_email": [],
        "missing_site": [],
        "stats": {},
        "errors": [],
    }
    raw_path(path, stamp).write_text("", encoding="utf-8")
    save_json(path, data)
    return data


def raw_path(path: Path, session_id: str) -> Path:
    directory = path.parent / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}.jsonl"


def load_session(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read the saved session at {path}: {exc}")
        return None
    if not isinstance(data, dict) or data.get("stage") in {None, "imported"}:
        return None
    return data


def save_session(path: Path, data: dict) -> None:
    save_json(path, data)


def append_raw(path: Path, session: dict, platform: str, keyword: str, items: list[dict]) -> None:
    destination = raw_path(path, session["id"])
    with destination.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps({"platform": platform, "keyword": keyword, "item": item}) + "\n")
    completed = [platform, keyword]
    if completed not in session["completed_jobs"]:
        session["completed_jobs"].append(completed)
    save_session(path, session)


def load_raw(path: Path, session: dict) -> list[tuple[str, str, list[dict]]]:
    destination = raw_path(path, session.get("id", ""))
    grouped: dict[tuple[str, str], list[dict]] = {}
    if not destination.exists():
        return []
    for line in destination.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"Skipped a damaged saved result: {exc}")
            continue
        key = (payload.get("platform") or "", payload.get("keyword") or "")
        grouped.setdefault(key, []).append(payload.get("item") or {})
    return [(platform, keyword, items) for (platform, keyword), items in grouped.items()]


def store_discovery(path: Path, session: dict, discovery) -> dict:
    session["stage"] = "ready"
    session["qualified"] = [lead_to_dict(lead) for lead in discovery.qualified]
    session["missing_email"] = [lead_to_dict(lead) for lead in discovery.missing_email]
    session["missing_site"] = [lead_to_dict(lead) for lead in discovery.missing_site]
    session["stats"] = discovery.stats
    session["errors"] = list(discovery.stats.get("errors") or [])
    save_session(path, session)
    return session


def leads_from(session: dict, key: str) -> list[Lead]:
    return [lead_from_dict(item) for item in session.get(key) or []]


def archive_session(path: Path, session: dict) -> None:
    if not session:
        return
    destination = path.parent / "sessions" / f"{session.get('id', 'session')}.json"
    save_json(destination, session)
    if path.exists():
        path.unlink()
