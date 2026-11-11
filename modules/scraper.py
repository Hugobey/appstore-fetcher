"""Discover apps through Apify, then normalize the actor fields we actually receive."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from modules.config import ROOT, project_path
from modules.dates import iso_date
from modules.deduplication import canonical_url
from modules.enrichment import normalize_website
from modules.errors import ApifyError
from modules.http_utils import HttpError, redact, request_with_retry
from modules.models import EmailFound, Lead

LOGGER = logging.getLogger("prospecting.scraper")
API_ROOT = "https://api.apify.com/v2"
TERMINAL_FAILURES = {"FAILED", "TIMED-OUT", "ABORTED"}
SOURCE_DROP = {
    "screenshotUrls",
    "ipadScreenshotUrls",
    "appletvScreenshotUrls",
    "supportedDevices",
    "screenshots",
    "reviews",
    "reviewsList",
    "similarApps",
    "descriptionHTML",
}


def keyword_plan(targeting: dict, settings: dict) -> list[str]:
    """Round-robin categories so one category cannot spend the whole search budget."""
    keyword_map = settings.get("category_keywords") or {}
    limit = int(settings["apify"]["max_searches_per_run"])
    pools: list[deque[str]] = []
    for category in targeting.get("categories") or []:
        words = [str(word).strip() for word in keyword_map.get(category) or [] if str(word).strip()]
        if not words and category != "Other":
            words = [category]
        if words:
            pools.append(deque(words))
    custom = [str(word).strip() for word in targeting.get("custom_keywords") or [] if str(word).strip()]
    if custom:
        pools.append(deque(custom))
    planned: list[str] = []
    seen = set()
    while len(planned) < limit and any(pools):
        for pool in pools:
            if not pool or len(planned) >= limit:
                continue
            word = pool.popleft()
            key = word.lower()
            if key in seen:
                continue
            seen.add(key)
            planned.append(word)
    return planned


def search_jobs(targeting: dict, settings: dict) -> list[tuple[str, str]]:
    platforms = _platforms(targeting.get("platforms"))
    jobs = []
    for keyword in keyword_plan(targeting, settings):
        for platform in platforms:
            jobs.append((platform, keyword))
    return jobs[: int(settings["apify"]["max_searches_per_run"])]


def _platforms(value: str | None) -> list[str]:
    if value == "android":
        return ["android"]
    if value == "both":
        return ["ios", "android"]
    return ["ios"]


def estimate_cost(jobs: list[tuple[str, str]], settings: dict) -> float:
    apify = settings["apify"]
    items = int(apify["max_items_per_search"])
    item_fee = float(apify["dataset_item_fee_usd"])
    total = 0.0
    for platform, _keyword in jobs:
        fee = float(apify["ios_search_fee_usd"] if platform == "ios" else apify["android_search_fee_usd"])
        total += fee + items * item_fee
    return round(total, 4)


def trim_source(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in SOURCE_DROP}


def _first(item: dict, *keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.lower() in {"free", "0"}:
        return 0.0
    try:
        return float(text.replace("$", ""))
    except ValueError:
        return None


def _int(value) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _compact_number(value) -> float | None:
    """Parse 100000, '100,000+', '$1.2M', or '20 thousand' style store fields."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    text = text.replace(",", "").replace("$", "").replace("+", "").replace("usd", "").strip()
    text = text.replace("billion", "b").replace("million", "m").replace("thousand", "k")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmb])?", text)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    elif suffix == "b":
        number *= 1_000_000_000
    return number


def _install_count(item: dict) -> int | None:
    """Google Play publishes minInstalls. The App Store actor does not publish downloads."""
    for key in ("minInstalls", "realInstalls"):
        number = _compact_number(item.get(key))
        if number is not None:
            return int(number)
    for key in ("installs", "downloads", "downloadCount"):
        number = _compact_number(item.get(key))
        if number is not None:
            return int(number)
    return None


def _revenue_usd(item: dict) -> float | None:
    """Use a revenue field when an actor sends one. The current actors do not."""
    for key in ("revenueUsd", "estimatedRevenue", "revenue", "minRevenue", "totalRevenue"):
        number = _compact_number(item.get(key))
        if number is not None:
            return number
    return None


def _bool_field(item: dict, *keys) -> bool | None:
    for key in keys:
        if key not in item or item[key] is None or item[key] == "":
            continue
        value = item[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    return None


def _is_error_item(item: dict) -> bool:
    if item.get("error") is True:
        return True
    code = str(item.get("code") or "")
    return code.startswith("C") and "trackId" not in item and "appId" not in item and "trackName" not in item


def normalize_app(item: dict, platform: str, keyword: str = "") -> Lead | None:
    if not isinstance(item, dict) or _is_error_item(item):
        return None
    if platform == "android" or item.get("appId"):
        return _normalize_android(item, keyword)
    return _normalize_ios(item, keyword)


def _normalize_ios(item: dict, keyword: str) -> Lead | None:
    if item.get("kind") not in (None, "", "software"):
        return None
    app_id = _first(item, "trackId", "collectionId", "id")
    if app_id is None:
        return None
    app_id = str(app_id).split(".")[0]
    if not app_id.isdigit():
        return None
    url = _first(item, "trackViewUrl", "collectionViewUrl", "url") or ""
    price = _number(_first(item, "price"))
    if price is None and str(_first(item, "formattedPrice") or "").lower() == "free":
        price = 0.0
    description = _first(item, "description", "descriptionText", "longDescription") or ""
    lead = Lead(
        platform="ios",
        app_store_id=app_id,
        app_name=str(_first(item, "trackName", "trackCensoredName", "name") or "").strip(),
        developer_name=str(_first(item, "artistName", "sellerName", "developer") or "").strip(),
        app_store_url=canonical_url(str(url), "ios", app_id),
        developer_website=normalize_website(_first(item, "sellerUrl", "sellerWebsite", "developerWebsite")),
        description=str(description).strip(),
        category=str(_first(item, "primaryGenreName", "genre") or "").strip(),
        release_date=iso_date(_first(item, "releaseDate", "released")),
        last_update=iso_date(_first(item, "currentVersionReleaseDate", "updated", "versionUpdated")),
        rating_count=_int(_first(item, "userRatingCount", "ratingCount")),
        review_count=_int(_first(item, "userReviewCount", "reviewCount")),
        average_rating=_number(_first(item, "averageUserRating", "score")),
        install_count=_install_count(item),
        revenue_usd=_revenue_usd(item),
        price=price,
        has_iap=_bool_field(item, "hasInAppPurchases", "inAppPurchases", "offersIAP", "iap"),
        search_keyword=keyword,
        source=trim_source(item),
    )
    # The iOS actor's reviews field is an array of written reviews, not a count.
    if lead.review_count is None and isinstance(item.get("reviews"), (int, float)):
        lead.review_count = int(item["reviews"])
    if not lead.app_name:
        return None
    return lead


def _normalize_android(item: dict, keyword: str) -> Lead | None:
    app_id = _first(item, "appId", "id")
    if not app_id:
        return None
    app_id = str(app_id).strip()
    url = _first(item, "url") or ""
    price = _number(_first(item, "price"))
    if price is None and item.get("free") is True:
        price = 0.0
    if price is None and str(_first(item, "priceText") or "").lower() == "free":
        price = 0.0
    description = _first(item, "description", "summary", "descriptionText") or ""
    website = normalize_website(_first(item, "developerWebsite", "developerUrl", "website", "privacyPolicy"))
    lead = Lead(
        platform="android",
        app_store_id=app_id,
        app_name=str(_first(item, "title", "name") or "").strip(),
        developer_name=str(_first(item, "developer", "developerLegalName", "artistName") or "").strip(),
        app_store_url=canonical_url(str(url), "android", app_id),
        developer_website=website,
        description=str(description).strip(),
        category=str(_first(item, "genre", "primaryGenreName") or "").strip(),
        release_date=iso_date(_first(item, "released", "releaseDate")),
        last_update=iso_date(_first(item, "updated", "currentVersionReleaseDate")),
        rating_count=_int(_first(item, "ratings", "userRatingCount", "ratingCount")),
        review_count=_int(_first(item, "reviews")) if not isinstance(item.get("reviews"), list) else None,
        average_rating=_number(_first(item, "score", "averageUserRating")),
        install_count=_install_count(item),
        revenue_usd=_revenue_usd(item),
        price=price,
        has_iap=_bool_field(item, "offersIAP", "inAppPurchases", "hasInAppPurchases", "adSupported"),
        search_keyword=keyword,
        source=trim_source(item),
    )
    # adSupported is advertising, not an in-app purchase. Only keep it when a real IAP key exists.
    if "offersIAP" not in item and "inAppPurchases" not in item and "hasInAppPurchases" not in item:
        lead.has_iap = _bool_field(item, "offersIAP", "inAppPurchases", "hasInAppPurchases")
    store_email = _first(item, "developerEmail", "email")
    if isinstance(store_email, str) and "@" in store_email:
        from modules.enrichment import classify_email

        email = store_email.strip().lower()
        email_type, evidence = classify_email(email, "", lead.developer_name, "unknown")
        lead.emails.append(
            EmailFound(email, email_type, lead.app_store_url or "Google Play listing", "Google Play listing. " + evidence)
        )
    if not lead.app_name:
        return None
    return lead


def apply_itunes_record(lead: Lead, record: dict) -> None:
    """Fill gaps from Apple's public lookup. Do not overwrite actor values."""
    if not lead.description:
        lead.description = str(record.get("description") or "").strip()
    if not lead.developer_website:
        lead.developer_website = normalize_website(record.get("sellerUrl"))
    if lead.rating_count is None:
        lead.rating_count = _int(record.get("userRatingCount"))
    if lead.average_rating is None:
        lead.average_rating = _number(record.get("averageUserRating"))
    if not lead.release_date:
        lead.release_date = iso_date(record.get("releaseDate"))
    if not lead.last_update:
        lead.last_update = iso_date(record.get("currentVersionReleaseDate"))
    if lead.price is None:
        lead.price = _number(record.get("price"))
    if not lead.category:
        lead.category = str(record.get("primaryGenreName") or "").strip()
    if not lead.app_store_url:
        lead.app_store_url = canonical_url(record.get("trackViewUrl"), "ios", lead.app_store_id)
    if not lead.developer_name:
        lead.developer_name = str(record.get("artistName") or record.get("sellerName") or "").strip()


def fill_from_itunes(leads: list[Lead], settings: dict, fetcher=None) -> None:
    if not settings["apify"].get("itunes_lookup_fallback", True):
        return
    pending = [
        lead
        for lead in leads
        if lead.platform == "ios"
        and (
            not lead.description
            or not lead.developer_website
            or lead.rating_count is None
            or not lead.release_date
            or not lead.last_update
        )
    ]
    if not pending:
        return
    fetch = fetcher or _itunes_fetch
    country = settings["apify"].get("country") or "us"
    by_id = {lead.app_store_id: lead for lead in pending}
    ids = list(by_id)
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        try:
            payload = fetch(batch, country)
        except Exception as exc:
            LOGGER.error("iTunes lookup failed: %s", exc)
            print(f"iTunes lookup failed: {exc}. Continuing with the Apify fields only.")
            return
        for record in payload.get("results") or []:
            app_id = str(record.get("trackId") or "")
            lead = by_id.get(app_id)
            if lead is not None:
                apply_itunes_record(lead, record)


def _itunes_fetch(app_ids: list[str], country: str) -> dict:
    response = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": ",".join(app_ids), "country": country},
        timeout=30,
        headers={"User-Agent": "AppProspectingCLI/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ApifyError("iTunes lookup returned an unexpected payload.")
    return data


class ApifyInterrupted(ApifyError):
    def __init__(self, message: str, items: list[dict]):
        super().__init__(message)
        self.items = items


class ApifyClient:
    def __init__(self, token: str, settings: dict, sleep=time.sleep):
        if not token:
            raise ApifyError(
                "No Apify token found. Set APIFY_TOKEN or put the token in credentials/apify_token. "
                "Scraping did not start."
            )
        self.token = token
        self.settings = settings
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        http = self.settings.get("http") or {}
        try:
            response = request_with_retry(
                self.session,
                method,
                API_ROOT + path,
                retries=int(http.get("retries", 4)),
                backoff_seconds=float(http.get("backoff_seconds", 1)),
                sleep=self.sleep,
                timeout=kwargs.pop("timeout", http.get("timeout_seconds", 30)),
                **kwargs,
            )
        except HttpError as exc:
            raise ApifyError(str(exc)) from exc
        if response.status_code in {401, 403}:
            raise ApifyError(
                "Apify rejected the token (HTTP "
                f"{response.status_code}). Check APIFY_TOKEN or credentials/apify_token."
            )
        if response.status_code == 402:
            raise ApifyError("Apify returned 402. The account does not have enough credit for this actor.")
        if response.status_code >= 400:
            raise ApifyError(self._error_text(response))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ApifyError(f"Apify returned non-JSON from {path}.") from exc

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            elif error:
                message = str(error)
            message = message or str(body.get("message") or "")
        message = message or redact(response.text[:400])
        return f"Apify HTTP {response.status_code}: {message}"

    def account_summary(self) -> dict:
        """Return only non-secret account fields. The raw payload can contain a proxy password."""
        payload = self._request("GET", "/users/me")
        data = payload.get("data") if isinstance(payload, dict) else {}
        plan = (data or {}).get("plan") or {}
        return {
            "username": (data or {}).get("username"),
            "tier": plan.get("tier"),
            "is_paying": plan.get("isPayingCustomer"),
        }

    def limits(self) -> dict:
        payload = self._request("GET", "/users/me/limits")
        data = payload.get("data") if isinstance(payload, dict) else {}
        limits = (data or {}).get("limits") or {}
        current = (data or {}).get("current") or {}
        remaining = None
        max_usd = limits.get("maxMonthlyUsageUsd")
        used = current.get("monthlyUsageUsd")
        if max_usd is not None and used is not None:
            remaining = round(float(max_usd) - float(used), 4)
        return {
            "max_monthly_usage_usd": max_usd,
            "monthly_usage_usd": used,
            "remaining_usd": remaining,
            "cycle": (data or {}).get("monthlyUsageCycle") or {},
        }

    def search(self, platform: str, keyword: str, country: str) -> tuple[list[dict], dict]:
        cached = read_cache(platform, keyword, country, self.settings)
        if cached is not None:
            return cached, {"cached": True, "usage_usd": 0}
        actor = self.settings["apify"]["ios_actor" if platform == "ios" else "android_actor"]
        run_input = self._input(platform, keyword, country)
        items, meta = self.run_actor(actor, run_input)
        write_cache(platform, keyword, country, self.settings, items)
        meta["cached"] = False
        return items, meta

    def _input(self, platform: str, keyword: str, country: str) -> dict:
        apify = self.settings["apify"]
        if platform == "ios":
            return {
                "term": keyword,
                "mode": "search",
                "country": country,
                "lang": store_lang(apify, "ios"),
                "page": 1,
                "endPage": int(apify.get("end_page") or 1),
                "maxItems": int(apify["max_items_per_search"]),
                "includeReviews": False,
                "includeSimilarApps": False,
            }
        return {
            "search": keyword,
            "country": country.upper(),
            "lang": store_lang(apify, "android"),
            "price": "all",
            "maxItems": int(apify["max_items_per_search"]),
            "includeReviews": False,
        }

    def run_actor(self, actor_id: str, run_input: dict) -> tuple[list[dict], dict]:
        apify = self.settings["apify"]
        params = {
            "timeout": int(apify.get("run_timeout_seconds") or 240),
            "maxTotalChargeUsd": float(apify.get("max_charge_usd_per_search") or 0.25),
        }
        LOGGER.info("Starting %s with input %s", actor_id, json.dumps(run_input))
        try:
            started = self._request("POST", f"/actors/{actor_id}/runs", params=params, json=run_input)
        except KeyboardInterrupt:
            raise
        run = started.get("data") if isinstance(started, dict) else None
        if not isinstance(run, dict) or not run.get("id"):
            raise ApifyError(f"Apify did not return a run id for {actor_id}.")
        try:
            finished = self._poll(run)
        except KeyboardInterrupt:
            self._abort(run.get("id"))
            partial = self._dataset_items(run.get("defaultDatasetId"))
            raise ApifyInterrupted(
                f"Stopped the Apify run {run.get('id')}. Retrieved {len(partial)} item(s) before the abort.",
                partial,
            ) from None
        status = finished.get("status")
        if status != "SUCCEEDED":
            message = finished.get("statusMessage") or status
            console = finished.get("consoleUrl") or ""
            partial = self._dataset_items(finished.get("defaultDatasetId"))
            if partial:
                LOGGER.error("Run %s ended %s. Keeping %s partial items.", run["id"], status, len(partial))
                print(f"Apify run ended as {status}: {message}. Keeping {len(partial)} item(s) already stored.")
                return partial, {"usage_usd": finished.get("usageTotalUsd"), "status": status, "console": console}
            raise ApifyError(f"Apify run {run['id']} {status}: {message}. {console}".strip())
        items = self._dataset_items(finished.get("defaultDatasetId"))
        usage = finished.get("usageTotalUsd")
        if usage is not None:
            print(f"Apify reported ${float(usage):.4f} for this search.")
        return items, {"usage_usd": usage, "status": status, "console": finished.get("consoleUrl")}

    def _poll(self, run: dict) -> dict:
        interval = float(self.settings["apify"].get("poll_interval_seconds") or 3)
        timeout = float(self.settings["apify"].get("run_timeout_seconds") or 240) + 30
        deadline = time.monotonic() + timeout
        current = run
        while time.monotonic() < deadline:
            status = current.get("status")
            if status == "SUCCEEDED" or status in TERMINAL_FAILURES:
                return current
            self.sleep(interval)
            payload = self._request("GET", f"/actor-runs/{run['id']}")
            current = payload.get("data") if isinstance(payload, dict) else current
        self._abort(run.get("id"))
        raise ApifyError(f"Apify run {run.get('id')} did not finish within {int(timeout)} seconds. It was aborted.")

    def _abort(self, run_id: str | None) -> None:
        if not run_id:
            return
        try:
            self._request("POST", f"/actor-runs/{run_id}/abort")
        except Exception as exc:
            LOGGER.warning("Could not abort Apify run %s: %s", run_id, exc)

    def _dataset_items(self, dataset_id: str | None) -> list[dict]:
        if not dataset_id:
            return []
        items: list[dict] = []
        offset = 0
        while True:
            payload = self._request(
                "GET",
                f"/datasets/{dataset_id}/items",
                params={"format": "json", "clean": "true", "limit": 1000, "offset": offset},
            )
            page = payload if isinstance(payload, list) else payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise ApifyError(f"Dataset {dataset_id} did not return a list of items.")
            items.extend(item for item in page if isinstance(item, dict))
            if len(page) < 1000:
                return items
            offset += 1000


def store_lang(apify: dict, platform: str) -> str:
    """Language code the store actor actually accepts.

    The App Store actor's prefill is en-us. Sending en makes a keyword search
    succeed and return zero apps. Google Play's prefill is en.
    """
    configured = str(apify.get("lang") or "").strip()
    if platform == "ios":
        if configured.lower() in {"", "en"}:
            return "en-us"
        return configured
    if configured.lower() in {"", "en-us", "en_us"}:
        return "en"
    return configured


def usable_results(items: list) -> bool:
    if not items:
        return False
    return not all(isinstance(item, dict) and set(item) == {"demo"} for item in items)


def cache_path(platform: str, keyword: str, country: str, settings: dict) -> Path:
    apify = settings["apify"]
    raw = "|".join(
        [
            platform,
            country.lower(),
            keyword.strip().lower(),
            str(apify.get("max_items_per_search")),
            str(apify.get("end_page")),
            store_lang(apify, platform),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return ROOT / "data" / "search_cache" / f"{digest}.json"


def read_cache(platform: str, keyword: str, country: str, settings: dict) -> list[dict] | None:
    path = cache_path(platform, keyword, country, settings)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Ignoring unreadable search cache {path.name}: {exc}")
        return None
    hours = float(settings["apify"].get("cache_hours") or 24)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > timedelta(hours=hours):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not usable_results(items):
        return None
    return items


def write_cache(platform: str, keyword: str, country: str, settings: dict, items: list[dict]) -> None:
    if not usable_results(items):
        return
    path = cache_path(platform, keyword, country, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "platform": platform,
        "keyword": keyword,
        "country": country,
        "items": items,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def missing_token_message() -> str:
    token_path = project_path("credentials/apify_token")
    return (
        "No Apify token found. Create an account token at https://console.apify.com/account/integrations "
        f"and either export APIFY_TOKEN or save it in {token_path}. Scraping did not start."
    )
