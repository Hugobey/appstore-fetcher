"""Persisted leads for one scouting run.

Analysis reloads these snapshots. It does not read the scout's live objects,
so a later in-memory edit cannot change what revenue enrichment sees.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from modules.models import Lead, lead_from_dict, lead_to_dict


class RunStore:
    def __init__(self, run_id: str, *, directory: Path | str | None = None):
        self.run_id = str(run_id or "run")
        self.directory = Path(directory) if directory else None
        self.path = None
        self._leads: list[dict] = []
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / f"{_safe_id(self.run_id)}.json"
            if self.path.exists():
                self._leads = _read_leads(self.path)

    def save(self, lead: Lead) -> None:
        payload = lead_to_dict(lead)
        key = lead.dedup_id()
        for index, item in enumerate(self._leads):
            if _key(item) == key:
                self._leads[index] = payload
                break
        else:
            self._leads.append(payload)
        self._flush()

    def load(self) -> list[Lead]:
        if self.path is not None and self.path.exists():
            self._leads = _read_leads(self.path)
        return [lead_from_dict(copy.deepcopy(item)) for item in self._leads]

    def __len__(self) -> int:
        return len(self._leads)

    def __bool__(self) -> bool:
        return True

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"run_id": self.run_id, "leads": self._leads}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _key(item: dict) -> str:
    return f"{item.get('platform')}:{item.get('app_store_id')}".lower()


def _safe_id(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)
    return cleaned or "run"


def _read_leads(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError(f"Could not read saved leads from {path}: {exc}") from exc
    leads = data.get("leads") if isinstance(data, dict) else None
    if not isinstance(leads, list):
        raise OSError(f"Saved leads in {path} are missing a leads list.")
    return leads
