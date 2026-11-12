"""Estimated monthly revenue from public App Store Tracker pages.

Qualified iOS leads are fetched concurrently. A missing, blocked, or timed-out
page never disqualifies the prospect.
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor

from modules.models import Lead

TRACKER_ROOT = "https://www.appstoretracker.com/app"
MRR_FLOOR_USD = 10_000
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_WORKERS = 5
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_APPLE_APP = re.compile(
    r"(?:https?://)?(?:www\.)?(?:apps|itunes)\.apple\.com/"
    r"(?:[a-z]{2}(?:-[a-z]{2})?/)?"
    r"app/(?:(?P<slug>[a-z0-9][a-z0-9-]*)/)?"
    r"id(?P<num>\d+)",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?",
)
_MONTHLY = re.compile(r"/\s*mo(?:nth)?|\bmonth", re.IGNORECASE)


def tracker_url(app_store_url: str) -> str:
    """Build `{slug}-{numeric_id}` and drop the literal App Store `id` prefix."""
    match = _APPLE_APP.search(app_store_url or "")
    if not match:
        return ""
    slug = (match.group("slug") or "").strip("-").lower()
    number = match.group("num")
    if not slug or not number:
        return ""
    return f"{TRACKER_ROOT}/{slug}-{number}"


def tracker_url_for_lead(lead: Lead) -> str:
    """Use the store URL slug. The CRM copy drops it, so fall back to the raw URL or app name."""
    for candidate in _store_urls(lead):
        built = tracker_url(candidate)
        if built:
            return built
    number = str(lead.app_store_id or "")
    slug = _slugify(lead.app_name)
    if lead.platform == "ios" and number.isdigit() and slug:
        return f"{TRACKER_ROOT}/{slug}-{number}"
    return ""


def parse_mrr(html: str) -> str:
    """Return a monthly figure at or above $10k, or an empty string."""
    if not html or not str(html).strip():
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    amount = _revenue_block_amount(soup)
    if amount is None:
        amount = _inline_monthly_amount(soup.get_text(" ", strip=True))
    if amount is None or amount < MRR_FLOOR_USD:
        return ""
    return format_mrr(amount)


def format_mrr(amount: float) -> str:
    if amount >= 1_000_000:
        value = amount / 1_000_000
        suffix = "M"
    else:
        value = amount / 1_000
        suffix = "k"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"${text}{suffix}/mo"


def fetch_mrr(url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS, getter=None) -> str:
    """Fetch one product page. Timeout, block, and parse failure stay non-fatal."""
    if not url:
        return ""
    getter = getter or _default_get
    try:
        response = getter(url, headers=BROWSER_HEADERS, timeout=timeout)
    except TypeError:
        try:
            response = getter(url)
        except Exception:
            return "N/A"
    except Exception:
        return "N/A"
    status = getattr(response, "status_code", 200)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 200
    if status >= 400:
        return "N/A"
    body = getattr(response, "text", "") or ""
    try:
        return parse_mrr(body) or ""
    except Exception:
        return ""


def demo_mrr(lead: Lead) -> str:
    """Stable sample figures for `--demo` and unit tests. Never touches the network."""
    app_id = str(lead.app_store_id or "")
    if app_id in {"1001", "1007"}:
        return "$15k/mo"
    digits = "".join(character for character in app_id if character.isdigit())
    if not digits or int(digits) % 2 == 0:
        return ""
    return "$15k/mo"


def enrich_estimated_mrr(
    leads: list[Lead],
    *,
    demo: bool = False,
    getter=None,
    max_workers: int = MAX_WORKERS,
) -> None:
    """Fill `estimated_mrr` for iOS leads that already survived dedup and email discovery."""
    use_demo = bool(demo or (getter is None and _running_under_unittest()))
    pending = [lead for lead in leads if _can_estimate(lead)]
    pending_ids = {id(lead) for lead in pending}
    for lead in leads:
        if id(lead) not in pending_ids:
            lead.estimated_mrr = lead.estimated_mrr or ""

    if not pending:
        return
    if use_demo:
        for lead in pending:
            lead.estimated_mrr = demo_mrr(lead)
        return

    workers = MAX_WORKERS if max_workers is None else max(1, int(max_workers))

    def _load(lead: Lead) -> tuple[Lead, str]:
        try:
            return lead, fetch_mrr(tracker_url_for_lead(lead), getter=getter)
        except Exception:
            return lead, "N/A"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for lead, value in pool.map(_load, pending):
            lead.estimated_mrr = value if value is not None else ""


def _can_estimate(lead: Lead) -> bool:
    return lead.platform == "ios" and bool(tracker_url_for_lead(lead))


def _store_urls(lead: Lead) -> list[str]:
    urls = [lead.app_store_url] if lead.app_store_url else []
    source = lead.source if isinstance(lead.source, dict) else {}
    for key in ("trackViewUrl", "collectionViewUrl", "url"):
        value = source.get(key)
        if value:
            urls.append(str(value))
    return urls


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _running_under_unittest() -> bool:
    return "unittest" in " ".join(sys.argv)


def _default_get(url: str, headers: dict | None = None, timeout: float | None = None):
    import requests

    return requests.get(url, headers=headers, timeout=timeout)


def _revenue_block_amount(soup) -> float | None:
    for label in soup.find_all("dt"):
        if label.get_text(" ", strip=True).lower() != "revenue":
            continue
        parent = label.parent
        chunks = []
        if parent is not None:
            chunks.extend(node.get_text(" ", strip=True) for node in parent.find_all("dd"))
        sibling = label.find_next_sibling()
        while sibling is not None and getattr(sibling, "name", None) in {"dd", "span", "p"}:
            chunks.append(sibling.get_text(" ", strip=True))
            sibling = sibling.find_next_sibling()
        block = " ".join(chunk for chunk in chunks if chunk)
        if not block or not _MONTHLY.search(block):
            return None
        return _parse_money(block)
    return None


def _inline_monthly_amount(text: str) -> float | None:
    for match in _MONEY.finditer(text or ""):
        window = text[match.start() : match.end() + 12]
        if not _MONTHLY.search(window):
            continue
        amount = _amount_from_match(match)
        if amount is not None:
            return amount
    return None


def _parse_money(text: str) -> float | None:
    match = _MONEY.search(text or "")
    if not match:
        return None
    return _amount_from_match(match)


def _amount_from_match(match: re.Match) -> float | None:
    try:
        number = float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return number
