"""Date parsing for App Store and Google Play fields."""

from __future__ import annotations

from datetime import date, datetime, timezone

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(value) -> date | None:
    """Return a calendar date, or None when the value is missing or unrecognized."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000.0
        if number < 1_000_000_000:
            return None
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        iso_candidate = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(iso_candidate).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso_date(value) -> str | None:
    parsed = parse_date(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def days_since(iso_value: str | None, today: date | None = None) -> int | None:
    parsed = parse_date(iso_value)
    if parsed is None:
        return None
    current = today or utc_today()
    delta = (current - parsed).days
    return 0 if delta < 0 else delta


def days_ago_label(iso_value: str | None, today: date | None = None) -> str:
    days = days_since(iso_value, today)
    if days is None:
        return "unknown"
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"
