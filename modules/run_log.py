"""Structured logs for one prospecting run.

Lines go to the in-memory event list, to logs/prospecting.log when the root
logger has a file handler, to stderr when console logging is on, and to an
optional per-run file. Secrets are redacted before anything is written.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"
    r"|((?:api[_-]?key|access[_-]?token|apify_token|bearer)\s*[=:]\s*)\S+"
    r"|(://[^:/\s]+:)[^@\s]+@"
)
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _redact(text: str) -> str:
    def replace(match: re.Match) -> str:
        if match.group(1):
            return match.group(1) + "[redacted]"
        if match.group(2):
            return match.group(2) + "[redacted]"
        return (match.group(3) or "") + "[redacted]@"

    return _SECRET.sub(replace, text)


def _format(record: dict) -> str:
    parts = [
        str(record.get("ts") or ""),
        str(record.get("level") or "INFO"),
        f"run_id={record.get('run_id') or '-'}",
        f"phase={record.get('phase') or '-'}",
        f"event={record.get('event') or '-'}",
        f"lead_id={record.get('lead_id') or '-'}",
        f"app_id={record.get('app_id') or '-'}",
        f"elapsed_s={record.get('elapsed_s')}",
    ]
    if record.get("http_status") is not None:
        parts.append(f"http_status={record['http_status']}")
    if record.get("duration_s") is not None:
        parts.append(f"duration_s={record['duration_s']}")
    if record.get("endpoint"):
        parts.append(f"endpoint={record['endpoint']}")
    if record.get("error"):
        parts.append(f"error={record['error']}")
    skip = {
        "ts",
        "level",
        "run_id",
        "phase",
        "event",
        "lead_id",
        "app_id",
        "elapsed_s",
        "http_status",
        "duration_s",
        "endpoint",
        "error",
        "body",
    }
    for key in sorted(record):
        value = record[key]
        if key in skip or value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    if record.get("body"):
        parts.append(f"body={record['body']}")
    return _redact(" ".join(parts))


class RunLog:
    def __init__(self, run_id: str, *, path: Path | str | None = None, console: bool = False):
        self.run_id = str(run_id or "run")
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        self._started = time.perf_counter()
        self._logger = logging.getLogger(f"prospecting.run.{self.run_id}")
        self._logger.setLevel(logging.INFO)
        # Demo runs keep these events in memory. Live runs also reach logs/prospecting.log.
        self._logger.propagate = bool((console or path is not None) and logging.getLogger().handlers)
        if not self._logger.handlers:
            self._logger.addHandler(logging.NullHandler())
        if console and not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler) for handler in self._logger.handlers):
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(stream)

    def event(
        self,
        phase: str,
        name: str,
        *,
        level: str = "INFO",
        lead_id: str | None = None,
        app_id: str | None = None,
        elapsed: float | None = None,
        http_status: int | None = None,
        error: str | None = None,
        duration: float | None = None,
        endpoint: str | None = None,
        body: str | None = None,
        **fields,
    ) -> dict:
        level_name = str(level or "INFO").upper()
        if level_name not in _LEVELS:
            level_name = "INFO"
        if elapsed is None:
            elapsed = time.perf_counter() - self._started
        record = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level_name,
            "run_id": self.run_id,
            "phase": phase,
            "event": name,
            "lead_id": lead_id,
            "app_id": None if app_id is None else str(app_id),
            "elapsed_s": round(float(elapsed), 3),
            "http_status": http_status,
            "duration_s": None if duration is None else round(float(duration), 3),
            "endpoint": endpoint or "",
            "error": "" if error is None else str(error),
            "body": _truncate(body),
        }
        for key, value in fields.items():
            if key in record or value is None:
                continue
            record[key] = value
        line = _format(record)
        record["line"] = line
        self.events.append(record)
        getattr(self._logger, level_name.lower(), self._logger.info)(line)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record


def _truncate(body: str | None, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", body or "").strip()
    compact = _redact(compact)
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"
