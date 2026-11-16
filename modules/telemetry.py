"""Anonymous, non-blocking usage events.

The payload is event, user_profile, consider_mrr, categories, is_licensed,
and cli_version. License keys, emails, and instance ids are not fields.
"""

from __future__ import annotations

import os
import threading

CLI_VERSION = "1.4.0"
_FIELDS = ("event", "user_profile", "consider_mrr", "categories", "is_licensed", "cli_version")


def telemetry_disabled(no_telemetry: bool = False) -> bool:
    if no_telemetry:
        return True
    return os.environ.get("DO_NOT_TRACK", "").strip() == "1"


def telemetry_endpoint() -> str:
    override = os.environ.get("SCOUT_TELEMETRY_ENDPOINT", "").strip()
    if override:
        return override
    base = os.environ.get("SCOUT_API_ENDPOINT", "https://api.yourdomain.com/v1").strip().rstrip("/")
    return f"{base}/telemetry"


def build_event(
    event: str,
    *,
    user_profile: str = "",
    consider_mrr: bool = False,
    categories=None,
    is_licensed: bool = False,
) -> dict:
    return {
        "event": str(event),
        "user_profile": str(user_profile or ""),
        "consider_mrr": bool(consider_mrr),
        "categories": [str(item) for item in (categories or [])],
        "is_licensed": bool(is_licensed),
        "cli_version": CLI_VERSION,
    }


def emit_event(
    event: str,
    *,
    user_profile: str = "",
    consider_mrr: bool = False,
    categories=None,
    is_licensed: bool = False,
    no_telemetry: bool = False,
    poster=None,
) -> threading.Thread | None:
    """Fire-and-forget POST. The caller does not wait, and failures are ignored."""
    if telemetry_disabled(no_telemetry):
        return None
    payload = build_event(
        event,
        user_profile=user_profile,
        consider_mrr=consider_mrr,
        categories=categories,
        is_licensed=is_licensed,
    )
    if set(payload) != set(_FIELDS):
        return None

    def _send() -> None:
        try:
            if poster is not None:
                poster(payload, 2.0)
                return
            import requests

            requests.post(telemetry_endpoint(), json=payload, timeout=2.0)
        except Exception:
            return

    thread = threading.Thread(target=_send, name="scout-telemetry", daemon=True)
    thread.start()
    return thread
