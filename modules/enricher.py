"""Licensed calls to SCOUT_API_ENDPOINT.

End-user installs do not contain APIFY_TOKEN or proxy credentials. Those stay
on the server. A creator with APIFY_TOKEN in .env (or the environment, or the
gitignored credentials/apify_token file) keeps the local Apify and
App Store Tracker pipeline instead of this client.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from modules.auth import assert_same_machine, machine_fingerprint, save_credentials
from modules.config import project_path
from modules.errors import LicenseError
from modules.models import EmailFound, Lead, lead_from_dict

DEFAULT_ENDPOINT = "https://api.yourdomain.com/v1"
STATUS_MESSAGES = {
    401: "Invalid license key.",
    403: "License already active on another machine",
    409: "Another scan is currently running on this license key",
    429: "Another scan is currently running on this license key",
    402: "Monthly quota reached — upgrade or wait for cycle reset",
}
_TARGETING_FIELDS = (
    "user_profile",
    "consider_mrr",
    "developer_type",
    "developer_types",
    "platforms",
    "categories",
    "max_ratings",
    "min_ratings",
    "max_app_age",
    "last_update",
    "min_score",
    "target_prospects",
    "max_mrr",
    "country",
    "template_id",
    "pricing_model",
    "revenue_mode",
)
_APP_ID = re.compile(r"id(\d+)", re.IGNORECASE)


def scout_endpoint() -> str:
    raw = os.environ.get("SCOUT_API_ENDPOINT", DEFAULT_ENDPOINT).strip()
    return raw.rstrip("/") or DEFAULT_ENDPOINT


def dotenv_value(name: str, path: Path | None = None) -> str:
    """Read one key from the project .env. Missing files return an empty string."""
    path = path or project_path(".env")
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key.strip() != name:
            continue
        return value.strip().strip('"').strip("'")
    return ""


def local_apify_enabled() -> bool:
    """True when this checkout is allowed to call Apify directly.

    The distributed CLI does not ship a token. Local execution is the creator
    path: APIFY_TOKEN in the environment, in .env, or in credentials/apify_token.
    """
    if os.environ.get("APIFY_TOKEN", "").strip():
        return True
    if dotenv_value("APIFY_TOKEN"):
        return True
    token_path = project_path("credentials/apify_token")
    try:
        return bool(token_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def auth_headers(credentials: dict | None) -> dict[str, str]:
    data = credentials or {}
    return {
        "X-License-Key": str(data.get("license_key") or ""),
        "X-Instance-ID": str(data.get("instance_id") or ""),
        "X-Machine-Fingerprint": str(data.get("machine_fingerprint") or ""),
        "Accept": "application/json",
    }


def status_message(status: int) -> str | None:
    return STATUS_MESSAGES.get(status)


def checkout_url() -> str:
    value = os.environ.get("CHECKOUT_URL", "https://yourdomain.com/checkout").strip()
    return value or "https://yourdomain.com/checkout"


def redact(text: str, secret: str) -> str:
    if not text:
        return ""
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def public_targeting(targeting: dict | None) -> dict:
    source = targeting or {}
    return {key: source.get(key) for key in _TARGETING_FIELDS if key in source}


class ScoutClient:
    """HTTP client for activation, scans, and MRR. Tests pass a transport."""

    def __init__(self, credentials: dict | None = None, *, endpoint: str | None = None, transport=None):
        self.credentials = dict(credentials or {})
        self.endpoint = (endpoint or scout_endpoint()).rstrip("/")
        self.transport = transport or _default_transport

    def request(self, method: str, path: str, payload: dict | None = None, *, timeout: float = 60):
        headers = auth_headers(self.credentials)
        url = f"{self.endpoint}/{path.lstrip('/')}"
        response = self.transport(method, url, headers, payload, timeout)
        return _raise_for_status(response, str(self.credentials.get("license_key") or ""))

    def activate(self, license_key: str) -> dict:
        fingerprint = machine_fingerprint()
        key = license_key.strip()
        if not key:
            raise LicenseError("Enter a license key.")
        self.credentials = {
            "license_key": key,
            "instance_id": "",
            "machine_fingerprint": fingerprint,
        }
        response = self.request(
            "POST",
            "/licenses/activate",
            {
                "license_key": key,
                "instance_name": fingerprint,
                "machine_fingerprint": fingerprint,
            },
        )
        body = _json_body(response)
        if body.get("activated") is False:
            raise _activation_failure(body, key, getattr(response, "status_code", None))
        instance = body.get("instance") if isinstance(body.get("instance"), dict) else {}
        instance_id = str(instance.get("id") or body.get("instance_id") or "").strip()
        if not instance_id:
            raise _activation_failure(body, key, getattr(response, "status_code", None))
        saved = {
            "license_key": key,
            "instance_id": instance_id,
            "machine_fingerprint": fingerprint,
        }
        save_credentials(saved)
        self.credentials = saved
        return saved

    def scan(self, targeting: dict) -> dict:
        assert_same_machine(self.credentials)
        response = self.request("POST", "/scans", {"targeting": public_targeting(targeting)})
        return _json_body(response)

    def fetch_mrr(self, apps: list[dict]) -> dict:
        assert_same_machine(self.credentials)
        response = self.request("POST", "/mrr", {"apps": list(apps or [])})
        return _json_body(response)


def activate_license(license_key: str, *, transport=None, endpoint: str | None = None) -> dict:
    return ScoutClient(endpoint=endpoint, transport=transport).activate(license_key)


def leads_from_payload(payload: dict | None) -> list[Lead]:
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("leads") or payload.get("apps") or []
    leads = []
    for item in rows:
        if isinstance(item, dict):
            leads.append(lead_from_api(item))
    return leads


def lead_from_api(item: dict) -> Lead:
    """Accept a Lead dict or one 13-column CRM row from the API."""
    if item.get("platform") and item.get("app_store_id"):
        try:
            return lead_from_dict(item)
        except TypeError:
            pass
    url = str(item.get("Appstore URL") or item.get("App Store URL") or item.get("app_store_url") or "")
    name = str(item.get("Appname") or item.get("App Name") or item.get("app_name") or "")
    app_id = str(item.get("app_store_id") or item.get("App Store ID") or _numeric_id(url) or _slug(name))
    platform_text = str(item.get("Platform") or item.get("platform") or "iOS").lower()
    platform = "android" if platform_text.startswith("android") else "ios"
    email = str(item.get("Contact Email") or item.get("email") or "").strip()
    emails = []
    if email:
        emails.append(EmailFound(address=email, email_type="founder", source_url=url, evidence="Licensed scan"))
    contact = str(item.get("Contact") or item.get("contact_name") or "")
    mrr = str(item.get("Estimated MRR") or item.get("estimated_mrr") or "")
    ratings = item.get("Rating count", item.get("Ratings", item.get("rating_count")))
    reviews = item.get("Review count", item.get("Review Count", item.get("review_count")))
    return Lead(
        platform=platform,
        app_store_id=app_id,
        app_name=name,
        developer_name=str(item.get("developer_name") or ""),
        app_store_url=url,
        developer_website=str(item.get("Website") or item.get("Developer Website") or item.get("developer_website") or ""),
        category=str(item.get("Category") or item.get("category") or ""),
        rating_count=_whole(ratings),
        review_count=_whole(reviews),
        contact_name=contact,
        emails=emails,
        score_normalized=_whole(item.get("Lead Score") if "Lead Score" in item else item.get("score_normalized")),
        estimated_mrr=mrr,
        buy_signal=str(item.get("Buy Signal") or item.get("buy_signal") or ""),
        mrr_status="enriched" if mrr else "pending",
        enrichment_status="enriched" if email else "pending",
    )


def apply_mrr_rows(leads: list[Lead], payload: dict | None) -> None:
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("apps") or payload.get("leads") or []
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("app_store_id") or row.get("id") or "")
        if key:
            by_id[key] = row
    for lead in leads:
        row = by_id.get(str(lead.app_store_id))
        if row is None:
            continue
        display = str(row.get("estimated_mrr") or row.get("Estimated MRR") or "")
        lead.estimated_mrr = display
        status = str(row.get("mrr_status") or ("enriched" if display else "unavailable"))
        lead.mrr_status = status


def _numeric_id(url: str) -> str:
    match = _APP_ID.search(url or "")
    return match.group(1) if match else ""


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "app"


def _whole(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _json_body(response) -> dict:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _raise_for_status(response, secret: str):
    status = getattr(response, "status_code", None)
    if status in STATUS_MESSAGES:
        raise LicenseError(STATUS_MESSAGES[status], status=status)
    if status is not None and int(status) >= 400:
        body = _json_body(response)
        detail = str(body.get("error") or body.get("message") or "")
        message = redact(detail, secret) or f"Scout API request failed ({status})."
        raise LicenseError(message, status=int(status))
    return response


def _activation_failure(body: dict, secret: str, status) -> LicenseError:
    error = str(body.get("error") or body.get("message") or "")
    lowered = error.lower()
    code = int(status) if isinstance(status, int) else None
    if code in (401, 404) or "invalid" in lowered or "not found" in lowered:
        return LicenseError("Invalid license key.", status=code or 401)
    if code in (403, 409, 429) or "limit" in lowered or "another machine" in lowered:
        return LicenseError(STATUS_MESSAGES[403], status=code or 403)
    cleaned = redact(error, secret)
    return LicenseError(cleaned or "Activation failed.", status=code or 403)


def _default_transport(method, url, headers, payload, timeout):
    import requests

    return requests.request(method, url, headers=headers, json=payload, timeout=timeout)


# One download is inferred from this many new ratings. Retention compounds for at most 6 months.
DAYS_PER_MONTH = 365.25 / 12.0
_DEFAULT_BENCHMARK = {
    "downloads_per_rating": 45,
    "paid_conversion_rate": 0.022,
    "retention_multiplier_6m": 2.6,
    "blended_monthly_arpu": 5.00,
}
CATEGORY_BENCHMARKS = {
    "Finance": {
        "downloads_per_rating": 30,
        "paid_conversion_rate": 0.045,
        "retention_multiplier_6m": 3.3,
        "blended_monthly_arpu": 12.00,
    },
    "Business": {
        "downloads_per_rating": 30,
        "paid_conversion_rate": 0.045,
        "retention_multiplier_6m": 3.3,
        "blended_monthly_arpu": 12.00,
    },
    "Health & Fitness": {
        "downloads_per_rating": 35,
        "paid_conversion_rate": 0.040,
        "retention_multiplier_6m": 3.1,
        "blended_monthly_arpu": 10.50,
    },
    "Productivity": {
        "downloads_per_rating": 35,
        "paid_conversion_rate": 0.035,
        "retention_multiplier_6m": 3.0,
        "blended_monthly_arpu": 8.50,
    },
    "Education": {
        "downloads_per_rating": 40,
        "paid_conversion_rate": 0.032,
        "retention_multiplier_6m": 2.9,
        "blended_monthly_arpu": 8.00,
    },
    "Lifestyle": {
        "downloads_per_rating": 40,
        "paid_conversion_rate": 0.028,
        "retention_multiplier_6m": 2.7,
        "blended_monthly_arpu": 6.50,
    },
    "Photo & Video": {
        "downloads_per_rating": 40,
        "paid_conversion_rate": 0.028,
        "retention_multiplier_6m": 2.7,
        "blended_monthly_arpu": 6.50,
    },
    "Utilities": dict(_DEFAULT_BENCHMARK),
}
NO_IAP_LABEL = "N/A (No IAP)"
VELOCITY_SUFFIX = "Velocity Est."
GROSS_SUFFIX = "Gross Sales Est."


def benchmark_for(category: str | None) -> dict:
    found = CATEGORY_BENCHMARKS.get(str(category or "").strip())
    if found:
        return found
    return dict(_DEFAULT_BENCHMARK)


def months_live(release_date, today=None) -> float:
    """Months since launch. An unknown date uses the full 6-month cohort window."""
    from modules.dates import days_since

    days = days_since(release_date, today)
    if days is None:
        return 6.0
    return max(days / DAYS_PER_MONTH, 1.0 / 30.0)


def monthly_new_ratings(rating_count, recent_ratings_30d, release_date, today=None) -> float:
    if recent_ratings_30d is not None:
        try:
            return float(recent_ratings_30d)
        except (TypeError, ValueError):
            pass
    try:
        ratings = float(rating_count or 0)
    except (TypeError, ValueError):
        ratings = 0.0
    return ratings / months_live(release_date, today)


def cohort_mrr(monthly_new: float, months: float, benchmark: dict) -> int:
    """6-month cohort compound. `int` truncates; it does not round."""
    window = min(max(months, 0.0), 6.0) / 6.0
    effective = max(1.0, float(benchmark["retention_multiplier_6m"]) * window)
    raw = (
        float(monthly_new)
        * float(benchmark["downloads_per_rating"])
        * float(benchmark["paid_conversion_rate"])
        * effective
        * float(benchmark["blended_monthly_arpu"])
    )
    if raw <= 0:
        return 0
    return int(raw)


def format_monthly(amount: int, suffix: str) -> str:
    amount = int(amount)
    if amount >= 1000:
        if amount % 1000 == 0:
            text = f"${amount // 1000}k"
        else:
            text = f"${amount / 1000:.1f}k"
    else:
        text = f"${amount}"
    return f"~ {text}/mo ({suffix})"


def estimate_velocity_mrr(category, monthly_new: float, months: float) -> str:
    amount = cohort_mrr(monthly_new, months, benchmark_for(category))
    return format_monthly(amount, VELOCITY_SUFFIX)


def estimate_gross_sales(monthly_new: float, downloads_per_rating: float, price: float) -> str:
    try:
        amount = int(float(monthly_new) * float(downloads_per_rating) * float(price or 0))
    except (TypeError, ValueError):
        amount = 0
    if amount < 0:
        amount = 0
    return format_monthly(amount, GROSS_SUFFIX)


def tracker_decision(lead) -> str:
    """`tracker` only for confirmed IAP apps in the mid-tier chart window.

    Upfront paid, ad-supported, high-volume indie, and everyone else stay local.
    Unknown IAP is not treated as ad-supported. Unknown age is not high-volume.
    """
    from modules.dates import days_since

    price = getattr(lead, "price", None)
    if isinstance(lead, dict):
        price = lead.get("price", price)
    try:
        price_value = None if price is None or price == "" else float(price)
    except (TypeError, ValueError):
        price_value = None
    if price_value is not None and price_value > 0:
        return "gross_sales"
    has_iap = lead.get("has_iap") if isinstance(lead, dict) else getattr(lead, "has_iap", None)
    if isinstance(has_iap, str):
        has_iap = {"true": True, "false": False, "1": True, "0": False}.get(has_iap.strip().lower(), None)
    ratings = lead.get("rating_count") if isinstance(lead, dict) else getattr(lead, "rating_count", None)
    if ratings is None and isinstance(lead, dict):
        ratings = lead.get("Ratings") or lead.get("Rating count")
    try:
        rating_count = None if ratings is None or ratings == "" else int(float(ratings))
    except (TypeError, ValueError):
        rating_count = None
    release = None
    if isinstance(lead, dict):
        release = lead.get("release_date")
    else:
        release = getattr(lead, "release_date", None)
    age_days = days_since(release) if release else None
    high_volume = (
        rating_count is not None
        and rating_count > 250
        and age_days is not None
        and age_days < 12 * DAYS_PER_MONTH
    )
    if has_iap is False and (price_value is None or price_value == 0):
        return "no_iap"
    if (
        has_iap is True
        and rating_count is not None
        and 50 <= rating_count <= 2500
        and not high_volume
    ):
        return "tracker"
    return "velocity"


def offline_revenue_display(source, today=None) -> str:
    """Local estimate. Demo rows with day offsets always use this instead of a pasted figure."""
    from modules.dates import utc_today
    from datetime import timedelta

    current = today or utc_today()
    if isinstance(source, dict):
        category = source.get("Category") or source.get("category") or ""
        ratings = source.get("rating_count", source.get("Ratings", source.get("Rating count")))
        recent = source.get("recent_ratings_30d")
        price = source.get("price")
        has_iap = source.get("has_iap")
        if "released_days_ago" in source and source.get("released_days_ago") not in (None, ""):
            release = (current - timedelta(days=int(source["released_days_ago"]))).isoformat()
        else:
            release = source.get("release_date")
    else:
        category = getattr(source, "category", "") or ""
        ratings = getattr(source, "rating_count", None)
        recent = getattr(source, "recent_ratings_30d", None)
        price = getattr(source, "price", None)
        has_iap = getattr(source, "has_iap", None)
        release = getattr(source, "release_date", None)
    if isinstance(has_iap, str):
        has_iap = {"true": True, "false": False}.get(has_iap.strip().lower(), None)
    try:
        price_value = None if price is None or price == "" else float(price)
    except (TypeError, ValueError):
        price_value = None
    months = months_live(release, current)
    try:
        recent_value = None if recent is None or recent == "" else float(recent)
    except (TypeError, ValueError):
        recent_value = None
    monthly = monthly_new_ratings(ratings, recent_value, release, current)
    if price_value is not None and price_value > 0:
        return estimate_gross_sales(monthly, benchmark_for(category)["downloads_per_rating"], price_value)
    if has_iap is False and (price_value is None or price_value == 0):
        return NO_IAP_LABEL
    return estimate_velocity_mrr(category, monthly, months)
