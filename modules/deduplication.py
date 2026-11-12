"""Deduplicate on App Store ID first and canonical URL second."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from modules.models import Lead

APPLE_ID_RE = re.compile(r"/id(\d+)", re.IGNORECASE)
PLAY_HOSTS = {"play.google.com"}
APPLE_HOSTS = {"apps.apple.com", "itunes.apple.com", "geo.itunes.apple.com"}


def name_key(app_name: str | None, developer: str | None) -> str:
    """Secondary identity. Both sides are required so blank cells do not collapse unrelated apps."""
    name = re.sub(r"\s+", " ", (app_name or "").strip().lower())
    maker = re.sub(r"\s+", " ", (developer or "").strip().lower())
    if not name or not maker:
        return ""
    return f"{name}|{maker}"


def index_url(url: str | None, platform: str | None = None, app_id: str | None = None) -> str:
    """URL identity with the query string, country segment, and trailing slash removed."""
    if not app_id and url:
        parsed_platform, parsed_id = identity_from_url(url)
        platform = platform or parsed_platform
        app_id = parsed_id
    if app_id and platform == "ios":
        return f"https://apps.apple.com/app/id{app_id}".lower()
    if app_id and platform == "android":
        return f"https://play.google.com/store/apps/details?id={app_id}".lower()
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host in APPLE_HOSTS:
        ids = APPLE_ID_RE.findall(parsed.path or "")
        if ids and "/developer/" not in (parsed.path or "").lower():
            return f"https://apps.apple.com/app/id{ids[-1]}".lower()
    if host in PLAY_HOSTS:
        play_id = (parse_qs(parsed.query).get("id") or [None])[0]
        if play_id:
            return f"https://play.google.com/store/apps/details?id={play_id}".lower()
    cleaned = parsed._replace(fragment="", query="")
    return urlunparse(cleaned).rstrip("/").lower()


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
        self.names: set[str] = set()

    def add(
        self,
        platform: str | None,
        app_id: str | None,
        url: str | None = None,
        app_name: str | None = None,
        developer: str | None = None,
    ) -> None:
        if platform and app_id:
            self.ids.add(f"{platform}:{app_id}".lower())
        indexed = index_url(url, platform, app_id)
        if indexed:
            self.urls.add(indexed)
        named = name_key(app_name, developer)
        if named:
            self.names.add(named)

    def add_lead(self, lead: Lead) -> None:
        self.add(lead.platform, lead.app_store_id, lead.app_store_url, lead.app_name, lead.developer_name)

    def contains(
        self,
        platform: str | None,
        app_id: str | None,
        url: str | None = None,
        app_name: str | None = None,
        developer: str | None = None,
    ) -> bool:
        if platform and app_id and f"{platform}:{app_id}".lower() in self.ids:
            return True
        indexed = index_url(url, platform, app_id)
        if indexed and indexed in self.urls:
            return True
        named = name_key(app_name, developer)
        if named and named in self.names:
            return True
        return False

    def contains_lead(self, lead: Lead) -> bool:
        return self.contains(
            lead.platform,
            lead.app_store_id,
            lead.app_store_url,
            lead.app_name,
            lead.developer_name,
        )
