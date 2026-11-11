"""Deduplicate on App Store ID first and canonical URL second."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from modules.models import Lead

APPLE_ID_RE = re.compile(r"/id(\d+)", re.IGNORECASE)
PLAY_HOSTS = {"play.google.com"}
APPLE_HOSTS = {"apps.apple.com", "itunes.apple.com", "geo.itunes.apple.com"}


def canonical_url(url: str | None, platform: str | None = None, app_id: str | None = None) -> str:
    if app_id and platform == "ios":
        country = "us"
        if url:
            match = re.search(r"(?:apps|itunes)\.apple\.com/([a-z]{2})/", url, re.IGNORECASE)
            if match:
                country = match.group(1).lower()
        return f"https://apps.apple.com/{country}/app/id{app_id}"
    if app_id and platform == "android":
        return f"https://play.google.com/store/apps/details?id={app_id}"
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host in APPLE_HOSTS:
        ids = APPLE_ID_RE.findall(parsed.path or "")
        if ids and "/developer/" not in (parsed.path or "").lower():
            country_match = re.search(r"/([a-z]{2})/", parsed.path or "", re.IGNORECASE)
            country = country_match.group(1).lower() if country_match else "us"
            return f"https://apps.apple.com/{country}/app/id{ids[-1]}"
    if host in PLAY_HOSTS:
        query = parse_qs(parsed.query)
        play_id = (query.get("id") or [None])[0]
        if play_id:
            return f"https://play.google.com/store/apps/details?id={play_id}"
    cleaned = parsed._replace(fragment="", query=parsed.query)
    return urlunparse(cleaned).rstrip("/")


def identity_from_url(url: str | None) -> tuple[str | None, str | None]:
    """Return (platform, app id) parsed from a CRM URL."""
    if not url:
        return None, None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host in APPLE_HOSTS:
        if "/developer/" in path.lower() and "/app/" not in path.lower():
            return None, None
        ids = APPLE_ID_RE.findall(path)
        if ids:
            return "ios", ids[-1]
    if host in PLAY_HOSTS:
        play_id = (parse_qs(parsed.query).get("id") or [None])[0]
        if play_id:
            return "android", play_id
    return None, None


class DedupIndex:
    """In-memory set of prospects already stored or seen in this run."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.urls: set[str] = set()

    def add(self, platform: str | None, app_id: str | None, url: str | None = None) -> None:
        if platform and app_id:
            self.ids.add(f"{platform}:{app_id}".lower())
        canonical = canonical_url(url, platform, app_id)
        if canonical:
            self.urls.add(canonical.lower())
        elif url:
            self.urls.add(url.strip().lower())

    def add_lead(self, lead: Lead) -> None:
        self.add(lead.platform, lead.app_store_id, lead.app_store_url)

    def contains(self, platform: str | None, app_id: str | None, url: str | None = None) -> bool:
        if platform and app_id and f"{platform}:{app_id}".lower() in self.ids:
            return True
        canonical = canonical_url(url, platform, app_id)
        if canonical and canonical.lower() in self.urls:
            return True
        if url and url.strip().lower() in self.urls:
            return True
        return False

    def contains_lead(self, lead: Lead) -> bool:
        return self.contains(lead.platform, lead.app_store_id, lead.app_store_url)
