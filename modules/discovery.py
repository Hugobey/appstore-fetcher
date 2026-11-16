"""Deterministic buy signals. Numeric lead scores are not shown to the user."""

from __future__ import annotations

from datetime import timedelta

from modules.dates import days_since, utc_today
from modules.enricher import DAYS_PER_MONTH, monthly_new_ratings

SIGNAL_PAYWALL = "🚀 Launched Paywall"
SIGNAL_VELOCITY = "📈 High-Velocity Indie"
SIGNAL_SLEEPING = "💤 Sleeping Giant"
SIGNAL_EARLY = "🌱 Early Traction"

_NOT_SMALL = {"agency", "scaleup", "large"}


def buy_signal(lead, today=None) -> str:
    """First matching signal wins. An empty string means none of the rules fit."""
    facts = _facts(lead, today or utc_today())
    if _paywall(facts):
        return SIGNAL_PAYWALL
    if _high_velocity(facts):
        return SIGNAL_VELOCITY
    if _sleeping_giant(facts):
        return SIGNAL_SLEEPING
    if _early_traction(facts):
        return SIGNAL_EARLY
    return ""


def _facts(lead, today) -> dict:
    if isinstance(lead, dict):
        ratings = _whole(lead.get("rating_count", lead.get("Ratings", lead.get("Rating count"))))
        recent = _whole(lead.get("recent_ratings_30d"))
        release = _offset_date(lead, "released_days_ago", today) or _text(lead.get("release_date"))
        updated = _offset_date(lead, "updated_days_ago", today) or _text(lead.get("last_update"))
        version = _offset_date(lead, "version_days_ago", today) or _text(
            lead.get("current_version_release_date")
        )
        if not version:
            version = updated
        has_iap = _boolish(lead.get("has_iap"))
        kind = _text(lead.get("developer_type")) or "unknown"
        return {
            "ratings": ratings,
            "recent": recent,
            "release": release,
            "updated": updated,
            "version": version,
            "has_iap": has_iap,
            "developer_type": kind,
            "today": today,
        }
    version = _text(getattr(lead, "current_version_release_date", None)) or _text(getattr(lead, "last_update", None))
    return {
        "ratings": getattr(lead, "rating_count", None),
        "recent": getattr(lead, "recent_ratings_30d", None),
        "release": _text(getattr(lead, "release_date", None)),
        "updated": _text(getattr(lead, "last_update", None)),
        "version": version,
        "has_iap": getattr(lead, "has_iap", None),
        "developer_type": _text(getattr(lead, "developer_type", None)) or "unknown",
        "today": today,
    }


def _paywall(facts: dict) -> bool:
    ratings = facts["ratings"]
    recent = facts["recent"]
    if ratings is None or ratings <= 150 or facts["has_iap"] is not True or recent is None:
        return False
    if not _within(facts["version"], 60, facts["today"]):
        return False
    months = _months(facts["release"], facts["today"])
    if months is None:
        return False
    historical = ratings / months
    if historical <= 0:
        return False
    return float(recent) >= 2.5 * historical


def _high_velocity(facts: dict) -> bool:
    ratings = facts["ratings"]
    if ratings is None or not 50 <= ratings <= 2500:
        return False
    if not _within_months(facts["release"], 18, facts["today"]):
        return False
    if not _is_small(facts["developer_type"]):
        return False
    monthly = monthly_new_ratings(ratings, facts["recent"], facts["release"], facts["today"])
    return monthly >= 15


def _sleeping_giant(facts: dict) -> bool:
    ratings = facts["ratings"]
    if ratings is None or ratings <= 500:
        return False
    if facts["has_iap"] is True:
        return False
    if not _older_than_months(facts["release"], 24, facts["today"]):
        return False
    return _within(facts["updated"], 90, facts["today"])


def _early_traction(facts: dict) -> bool:
    ratings = facts["ratings"]
    if ratings is None or not 10 <= ratings <= 149:
        return False
    if not _within_months(facts["release"], 12, facts["today"]):
        return False
    return _within(facts["updated"], 60, facts["today"])


def _is_small(kind: str) -> bool:
    return (kind or "").strip().lower() not in _NOT_SMALL


def _within(iso_value: str | None, limit: int, today) -> bool:
    age = days_since(iso_value, today)
    return age is not None and age <= limit


def _within_months(iso_value: str | None, months: float, today) -> bool:
    age = days_since(iso_value, today)
    return age is not None and age <= months * DAYS_PER_MONTH


def _older_than_months(iso_value: str | None, months: float, today) -> bool:
    age = days_since(iso_value, today)
    return age is not None and age > months * DAYS_PER_MONTH


def _months(iso_value: str | None, today) -> float | None:
    age = days_since(iso_value, today)
    if age is None:
        return None
    return max(age / DAYS_PER_MONTH, 1.0 / 30.0)


def _offset_date(source: dict, key: str, today):
    if key not in source or source.get(key) in (None, ""):
        return None
    try:
        days = int(source[key])
    except (TypeError, ValueError):
        return None
    return (today - timedelta(days=days)).isoformat()


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _whole(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolish(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return {"true": True, "false": False, "1": True, "0": False}.get(value.strip().lower())
    return None
