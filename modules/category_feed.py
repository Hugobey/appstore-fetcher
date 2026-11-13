"""App Store category discovery. Charts come from public genre RSS, then iTunes Lookup.

There is no keyword search. A selected category maps to Apple's genre id. Page 1 is
the top free chart and any later page is the top paid chart for that same genre.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from modules.errors import ApifyError

LOGGER = logging.getLogger("prospecting.categories")

# Official iOS App Store genre ids. "Other" is not a store genre.
GENRE_IDS = {
    "Business": 6000,
    "Weather": 6001,
    "Utilities": 6002,
    "Travel": 6003,
    "Sports": 6004,
    "Social Networking": 6005,
    "Reference": 6006,
    "Productivity": 6007,
    "Photo & Video": 6008,
    "News": 6009,
    "Navigation": 6010,
    "Music": 6011,
    "Lifestyle": 6012,
    "Health & Fitness": 6013,
    "Games": 6014,
    "Finance": 6015,
    "Entertainment": 6016,
    "Education": 6017,
    "Books": 6018,
    "Medical": 6020,
    "Magazines & Newspapers": 6021,
    "Food & Drink": 6023,
    "Shopping": 6024,
    "Developer Tools": 6026,
    "Graphics & Design": 6027,
}
FREE_CHART = "topfreeapplications"
PAID_CHART = "toppaidapplications"
LOOKUP_BATCH = 180


def category_plan(targeting: dict, settings: dict | None = None, limit: int | None = None) -> list[str]:
    """Selected store genres, in the order the user picked them. Keywords are ignored."""
    del settings
    planned: list[str] = []
    seen: set[str] = set()
    for raw in targeting.get("categories") or []:
        name = str(raw).strip()
        key = name.lower()
        if not name or key in seen or name not in GENRE_IDS:
            continue
        seen.add(key)
        planned.append(name)
        if limit is not None and len(planned) >= int(limit):
            break
    return planned


def chart_for_page(page: int) -> str:
    return FREE_CHART if int(page) <= 1 else PAID_CHART


def chart_url(country: str, genre_id: int, chart: str) -> str:
    front = (country or "us").strip().lower()
    return f"https://itunes.apple.com/{front}/rss/{chart}/limit=200/genre={int(genre_id)}/json"


def lookup_url(country: str, app_ids: list[str]) -> str:
    front = (country or "us").strip().lower()
    joined = ",".join(app_ids)
    return f"https://itunes.apple.com/lookup?id={joined}&country={front}&entity=software"


def entry_ids(payload: dict) -> list[str]:
    """Numeric ids from a category RSS feed, in chart order."""
    feed = payload.get("feed") if isinstance(payload, dict) else None
    entries = (feed or {}).get("entry") if isinstance(feed, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    found: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        number = _entry_id(entry)
        if not number or number in seen:
            continue
        seen.add(number)
        found.append(number)
    return found


def lookup_records(payload: dict) -> list[dict]:
    """Software results from an iTunes Lookup payload. Artist rows are skipped."""
    rows = payload.get("results") if isinstance(payload, dict) else None
    apps = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if item.get("wrapperType") == "artist":
            continue
        if item.get("kind") not in (None, "", "software"):
            continue
        apps.append(item)
    return apps


def fetch_genre_apps(category: str, country: str, chart: str, getter: Callable[[str], dict]) -> list[dict]:
    """RSS ids, then one Lookup call per batch so sellerUrl and supportUrl are present."""
    genre_id = GENRE_IDS.get(category)
    if genre_id is None:
        return []
    feed = getter(chart_url(country, genre_id, chart))
    identifiers = entry_ids(feed if isinstance(feed, dict) else {})
    apps: list[dict] = []
    seen: set[str] = set()
    for start in range(0, len(identifiers), LOOKUP_BATCH):
        chunk = identifiers[start : start + LOOKUP_BATCH]
        payload = getter(lookup_url(country, chunk))
        for item in lookup_records(payload if isinstance(payload, dict) else {}):
            number = str(item.get("trackId") or item.get("collectionId") or "").split(".")[0]
            if not number or number in seen:
                continue
            seen.add(number)
            apps.append(item)
    return apps


def category_searcher(settings: dict, getter: Callable[[str], dict] | None = None):
    """`(platform, category, country, page)` searcher backed by genre RSS.

    Page 1 is top free. Any later page is top paid. Android has no genre RSS here.
    Results are cached with the existing search cache. Empty feeds are not cached.
    """

    def search(platform: str, category: str, country: str, page: int = 1):
        if platform != "ios" or category not in GENRE_IDS:
            return [], {"cached": False, "usage_usd": 0}
        chart = chart_for_page(page)
        cache_key = f"{category}|{chart}"
        if getter is None:
            from modules.scraper import read_cache, write_cache

            cached = read_cache(platform, cache_key, country, settings, 1)
            if cached is not None:
                return cached, {"cached": True, "usage_usd": 0}
        try:
            items = fetch_genre_apps(category, country, chart, getter or _json_get)
        except ApifyError:
            raise
        except Exception as exc:
            raise ApifyError(f"Category feed for {category} ({country}) failed: {exc}") from exc
        if getter is None and items:
            from modules.scraper import write_cache

            write_cache(platform, cache_key, country, settings, items, 1)
        return items, {"cached": False, "usage_usd": 0}

    return search


def _json_get(url: str) -> dict:
    import requests

    try:
        response = requests.get(
            url,
            timeout=20,
            headers={"Accept": "application/json", "User-Agent": "AppProspectingCLI/1.0"},
        )
    except requests.RequestException as exc:
        raise ApifyError(f"Could not reach Apple's category feed: {exc}") from exc
    if response.status_code >= 400:
        raise ApifyError(f"Apple's category feed returned HTTP {response.status_code} for {url}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApifyError("Apple's category feed did not return JSON.") from exc
    if not isinstance(payload, dict):
        raise ApifyError("Apple's category feed returned an unexpected payload.")
    return payload


def _entry_id(entry: dict) -> str:
    raw = entry.get("id")
    if isinstance(raw, dict):
        attributes = raw.get("attributes") or {}
        number = str(attributes.get("im:id") or "").strip()
        if number.isdigit():
            return number
        label = str(raw.get("label") or "")
    else:
        label = str(raw or "")
    digits = ""
    marker = label.lower().rfind("/id")
    if marker >= 0:
        digits = "".join(character for character in label[marker + 3 :] if character.isdigit())
    return digits if digits.isdigit() else ""
