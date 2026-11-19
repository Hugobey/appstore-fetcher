"""Estimated monthly revenue from public App Store Tracker pages.

The shortlist is fetched one app at a time. A missing, unverified, or
rate-limited page stays blank and never fills another app's row.
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from modules.enricher import NO_IAP_LABEL, bucket_mrr_amount, offline_revenue_display, tracker_decision
from modules.models import Lead

TRACKER_ROOT = "https://www.appstoretracker.com/app"
MRR_FLOOR_USD = 10_000
REQUEST_TIMEOUT_SECONDS = 5.0
PAUSE_MIN_SECONDS = 1.5
PAUSE_MAX_SECONDS = 2.5
APIFY_PROXY = "http://auto:{token}@proxy.apify.com:8000"
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
_TRACKER_APP = re.compile(r"/app/([a-z0-9-]+)-(\d+)(?:[/?#]|$)", re.IGNORECASE)
_APP_ID = re.compile(r"(?:/id|-)(\d{4,})(?:[/?#]|$)")
_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?",
)
# Sensor Tower's public bucket. The page prints this instead of a dollar amount.
_UNDER_5K = re.compile(r"^<\s*\$\s*5\s*k(?:\s*/\s*mo(?:nth)?)?(?:\s+mrr)?$", re.IGNORECASE)
_MONTHLY = re.compile(r"/\s*mo(?:nth)?|\bmonth", re.IGNORECASE)
_LABEL = re.compile(r"^(?:estimated\s+)?(?:monthly\s+)?(?:mrr|revenue)$", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)[\"']"
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"']og:title[\"']",
    re.IGNORECASE,
)
_CANONICAL = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"']"
    r"|<link[^>]+href=[\"']([^\"']+)[\"'][^>]*rel=[\"']canonical[\"']",
    re.IGNORECASE,
)
_OG_URL = re.compile(
    r"<meta[^>]+property=[\"']og:url[\"'][^>]*content=[\"']([^\"']+)[\"']"
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:url[\"']",
    re.IGNORECASE,
)
_EMPTY_TOKENS = {
    "-",
    "—",
    "–",
    "n/a",
    "na",
    "null",
    "none",
    "unknown",
    "$0",
    "$0.00",
    "0",
    "$0/mo",
    "$0k",
    "$0k/mo",
}


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
    """Return the third metric tile's figure, including a literal `<$5K`."""
    display, _numeric = read_mrr(html)
    return display


def read_mrr(html: str) -> tuple[str, int | None]:
    """Return `(display, dollars)` from the third `@container` metric tile.

    The tiles are rank, rating, then revenue. The app header is also
    `@container`, but it has no definition term, so it is not one of the three.
    A blurred or `aria-hidden` tile is the logged-out `$480K` placeholder and
    is not an estimate. `<$5K` is kept as displayed. Other amounts under $10k
    stay blank. `$0`, `-`, and `N/A` are unknown.
    """
    if not html or not str(html).strip():
        return "", None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "", None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return "", None
    tiles = _metric_containers(soup)
    if len(tiles) < 3:
        return "", None
    return _revenue_tile(tiles[2])


def format_mrr(amount: float) -> str:
    if amount >= 1_000_000:
        value = amount / 1_000_000
        suffix = "M"
    else:
        value = amount / 1_000
        suffix = "k"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"${text}{suffix}/mo"


def dollars_from_display(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text or text.upper() == "N/A" or _is_empty_marker(text) or _UNDER_5K.match(text):
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    amount = _amount_from_match(match)
    if amount is None or amount <= 0:
        return None
    return int(round(amount))


def fetch_mrr(url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS, getter=None) -> str:
    """Fetch one product page. Timeout and HTTP errors stay non-fatal."""
    display, _numeric = fetch_mrr_reading(url, timeout=timeout, getter=getter)
    return display


def fetch_mrr_reading(url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS, getter=None) -> tuple[str, int | None]:
    """Return one page's own figure. A redirect or a foreign canonical is blank."""
    if not url:
        return "", None
    getter = getter or _default_get
    try:
        response = getter(url, headers=BROWSER_HEADERS, timeout=timeout)
    except TypeError:
        try:
            response = getter(url)
        except Exception:
            return "", None
    except Exception:
        return "", None
    status = getattr(response, "status_code", 200)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 200
    if status == 429 or status >= 400:
        return "", None
    body = getattr(response, "text", "") or ""
    final_url = str(getattr(response, "url", "") or "")
    try:
        if not page_matches_app(url, body, final_url):
            return "", None
        return read_mrr(body)
    except Exception:
        return "", None


def page_matches_app(requested: str, body: str, final_url: str) -> bool:
    """True when this response is the requested app, not a homepage, 404, or other listing."""
    slug, number = _requested_identity(requested)
    if not number:
        return False
    if _is_missing_page(body):
        return False
    identities = _primary_identity_urls(body)
    if any(not _url_accepts(item, number) for item in identities):
        return False
    final = (final_url or "").strip()
    if final:
        if not _url_accepts(final, number):
            return False
        if _url_confirms(final, number):
            return True
    if any(_url_confirms(item, number) for item in identities):
        return True
    return _title_confirms(body, number, slug)


def demo_mrr(lead: Lead) -> str:
    """Stable sample figures for `--demo` and unit tests. Never touches the network."""
    app_id = str(lead.app_store_id or "")
    if app_id in {"1001", "1007"}:
        return "$15k/mo"
    digits = "".join(character for character in app_id if character.isdigit())
    if not digits or int(digits) % 2 == 0:
        return ""
    return "$15k/mo"


def enrich_one_mrr(
    lead: Lead,
    *,
    demo: bool = False,
    getter=None,
    pause=None,
    index: int = 0,
    on_item=None,
) -> str:
    """One tracker lookup for the streaming analyst.

    `index` is how many lookups this analyst has already started, so the
    1.5–2.5s pause applies between apps and not before the first. An injected
    getter does not wait unless `pause` is set. The returned display is `""`
    when the page is missing, unverified, timed out, or rate-limited.
    """
    use_demo = bool(demo or (getter is None and _running_under_unittest()))
    if on_item is not None:
        on_item(index, 1, lead)
    if not _can_estimate(lead):
        lead.estimated_mrr = lead.estimated_mrr or ""
        return lead.estimated_mrr or ""
    if use_demo:
        display = demo_mrr(lead)
        lead.estimated_mrr = display
        lead.mrr_numeric = dollars_from_display(display)
        return display or ""
    _wait_turn(index, pause, use_demo=False, getter=getter)
    try:
        display, numeric = fetch_mrr_reading(tracker_url_for_lead(lead), getter=getter)
    except Exception:
        display, numeric = "", None
    lead.estimated_mrr = display or ""
    lead.mrr_numeric = numeric
    return lead.estimated_mrr or ""


@dataclass
class MrrOutcome:
    """One revenue lookup. `status` is never a lead-rejection reason."""

    display: str = ""
    numeric: int | None = None
    status: str = "error"
    http_status: int | None = None
    error: str = ""
    duration_s: float = 0.0
    body: str = ""
    endpoint: str = ""


def apply_mrr_outcome(
    lead: Lead,
    *,
    demo: bool = False,
    getter=None,
    pause=None,
    index: int = 0,
    revenue_mode: str | None = None,
) -> MrrOutcome:
    """One revenue attempt. Failures do not clear a figure already on the lead.

    App Store Tracker is queried only for confirmed IAP apps in the mid-tier
    window. Upfront paid, ad-supported, and high-volume indie apps use the
    local formula. HTTP 429 is not retried. An injected getter does not sleep
    unless `pause` is set.
    """
    endpoint = tracker_url_for_lead(lead)
    mode = revenue_mode or "tracker"
    if mode == "skip":
        if lead.mrr_status == "pending":
            lead.mrr_status = "skipped"
        return MrrOutcome(status="skipped", endpoint="")
    if mode in {"no_iap", "gross_sales"}:
        return _assign_local(lead, mode, endpoint)
    decision = tracker_decision(lead)
    if decision != "tracker":
        if (lead.estimated_mrr or "").strip():
            return MrrOutcome(
                display=lead.estimated_mrr,
                numeric=lead.mrr_numeric,
                status=lead.mrr_status or "enriched",
                endpoint=endpoint,
            )
        return _assign_local(lead, decision, endpoint)
    use_demo = bool(demo or (getter is None and _running_under_unittest()))
    if not _can_estimate(lead):
        outcome = MrrOutcome(status="unavailable", endpoint=endpoint, error="no public tracker url")
        _assign_outcome(lead, outcome)
        return outcome
    if use_demo:
        display = demo_mrr(lead)
        outcome = MrrOutcome(
            display=display or "",
            numeric=dollars_from_display(display),
            status="enriched" if (display or "").strip() else "unavailable",
            endpoint=endpoint,
        )
        _assign_outcome(lead, outcome)
        return outcome
    _wait_turn(index, pause, use_demo=False, getter=getter)
    started = time.perf_counter()
    try:
        response = _invoke_getter(getter or _default_get, endpoint)
    except Exception as exc:
        http_status = _status_from_exception(exc)
        status_name = "rate_limited" if http_status == 429 else "timeout" if _is_timeout(exc) else "error"
        outcome = MrrOutcome(
            status=status_name,
            http_status=http_status,
            error=f"{type(exc).__name__}: {exc}",
            duration_s=time.perf_counter() - started,
            endpoint=endpoint,
        )
        _assign_outcome(lead, outcome)
        return outcome
    duration = time.perf_counter() - started
    http_status = _status_code(response)
    body = _safe_snippet(getattr(response, "text", "") or "")
    if http_status == 429:
        outcome = MrrOutcome(
            status="rate_limited",
            http_status=429,
            error="HTTP 429",
            duration_s=duration,
            body=body,
            endpoint=endpoint,
        )
        return _fallback_or_keep(lead, outcome)
    if http_status >= 400:
        outcome = MrrOutcome(
            status="error",
            http_status=http_status,
            error=f"HTTP {http_status}",
            duration_s=duration,
            body=body,
            endpoint=endpoint,
        )
        _assign_outcome(lead, outcome)
        return outcome
    try:
        full = getattr(response, "text", "") or ""
        final_url = str(getattr(response, "url", "") or "")
        if not page_matches_app(endpoint, full, final_url):
            outcome = MrrOutcome(
                status="unavailable",
                http_status=http_status,
                error="response was not this app",
                duration_s=duration,
                body=body,
                endpoint=endpoint,
            )
            return _fallback_or_keep(lead, outcome)
        display, numeric = read_mrr(full)
        if (display or "").strip():
            outcome = MrrOutcome(
                display=display,
                numeric=numeric,
                status="enriched",
                http_status=http_status,
                duration_s=duration,
                endpoint=endpoint,
            )
        else:
            outcome = MrrOutcome(
                status="unavailable",
                numeric=numeric,
                http_status=http_status,
                error="no usable estimate",
                duration_s=duration,
                body=body,
                endpoint=endpoint,
            )
            return _fallback_or_keep(lead, outcome)
    except Exception as exc:
        outcome = MrrOutcome(
            status="error",
            http_status=http_status,
            error=f"{type(exc).__name__}: {exc}",
            duration_s=duration,
            body=body,
            endpoint=endpoint,
        )
    _assign_outcome(lead, outcome)
    return outcome


def _assign_local(lead: Lead, kind: str, endpoint: str) -> MrrOutcome:
    if kind == "no_iap":
        display = NO_IAP_LABEL
        numeric = None
    else:
        display = offline_revenue_display(lead)
        numeric = None
    outcome = MrrOutcome(display=display, numeric=numeric, status="enriched", endpoint=endpoint)
    _assign_outcome(lead, outcome)
    return outcome


def _fallback_or_keep(lead: Lead, outcome: MrrOutcome) -> MrrOutcome:
    """429 and empty pages keep a saved figure. A blank cell gets the cohort formula."""
    if (lead.estimated_mrr or "").strip():
        _assign_outcome(lead, outcome)
        return outcome
    filled = offline_revenue_display(lead)
    lead.estimated_mrr = filled
    lead.mrr_numeric = None
    lead.mrr_status = "enriched"
    outcome.display = filled
    return outcome


def _assign_outcome(lead: Lead, outcome: MrrOutcome) -> None:
    """Record the attempt. Only a non-empty estimate replaces the saved figure."""
    if outcome.status == "enriched" and (outcome.display or "").strip():
        lead.estimated_mrr = outcome.display
        lead.mrr_numeric = outcome.numeric
    lead.mrr_status = outcome.status


def _invoke_getter(getter, url: str):
    try:
        return getter(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    except TypeError:
        return getter(url)


def _status_code(response) -> int:
    status = getattr(response, "status_code", 200)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 200


def _status_from_exception(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if "timeout" in type(exc).__name__.lower():
        return True
    try:
        import requests
    except ImportError:
        return False
    timeout_type = getattr(requests, "Timeout", None)
    return bool(timeout_type) and isinstance(exc, timeout_type)


def _safe_snippet(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    compact = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+", r"\1[redacted]", compact)
    compact = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|apify_token|bearer)\s*[=:]\s*)\S+",
        r"\1[redacted]",
        compact,
    )
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def enrich_estimated_mrr(
    leads: list[Lead],
    *,
    demo: bool = False,
    getter=None,
    max_workers: int | None = None,
    pause=None,
    on_item=None,
) -> None:
    """Fill `estimated_mrr` one lead at a time.

    `pause` is a seconds value or a zero-arg callable used between requests.
    Live lookups wait 1.5–2.5s. An injected getter does not wait unless `pause`
    is set, so unit tests stay fast. Each page is applied to that lead only.
    """
    del max_workers
    use_demo = bool(demo or (getter is None and _running_under_unittest()))
    pending = [lead for lead in leads if _can_estimate(lead)]
    pending_ids = {id(lead) for lead in pending}
    for lead in leads:
        if id(lead) not in pending_ids:
            lead.estimated_mrr = lead.estimated_mrr or ""

    if not pending:
        return
    if use_demo:
        for index, lead in enumerate(pending):
            if on_item is not None:
                on_item(index, len(pending), lead)
            display = demo_mrr(lead)
            lead.estimated_mrr = display
            lead.mrr_numeric = dollars_from_display(display)
        return

    for index, lead in enumerate(pending):
        if on_item is not None:
            on_item(index, len(pending), lead)
        _wait_turn(index, pause, use_demo=use_demo, getter=getter)
        lead_id = id(lead)
        try:
            display, numeric = fetch_mrr_reading(tracker_url_for_lead(lead), getter=getter)
        except Exception:
            display, numeric = "", None
        if id(lead) != lead_id:
            continue
        lead.estimated_mrr = display or ""
        lead.mrr_numeric = numeric


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


def apify_proxy_url() -> str:
    """Apify proxy URL when `APIFY_TOKEN` is set in the environment or `.env`."""
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        token = _dotenv_token()
    if not token:
        return ""
    return APIFY_PROXY.format(token=token)


def _dotenv_token() -> str:
    path = Path(__file__).resolve().parent.parent / ".env"
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
        if key.strip() != "APIFY_TOKEN":
            continue
        return value.strip().strip('"').strip("'")
    return ""


def _wait_turn(index: int, pause, *, use_demo: bool, getter) -> None:
    if index <= 0:
        return
    if callable(pause):
        pause()
        return
    if pause is not None:
        if float(pause) > 0:
            time.sleep(float(pause))
        return
    if getter is None and not use_demo:
        time.sleep(random.uniform(PAUSE_MIN_SECONDS, PAUSE_MAX_SECONDS))


def _default_get(url: str, headers: dict | None = None, timeout: float | None = None):
    import requests

    proxies = None
    proxy = apify_proxy_url()
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    return requests.get(url, headers=headers, timeout=timeout, proxies=proxies)


def _requested_identity(url: str) -> tuple[str, str]:
    match = _TRACKER_APP.search(url or "")
    if match:
        return match.group(1).lower(), match.group(2)
    found = _APP_ID.search(url or "")
    return "", found.group(1) if found else ""


def _tracker_number(url: str) -> str:
    _slug, number = _requested_identity(url)
    return number


def _url_confirms(url: str, number: str) -> bool:
    return bool(number) and _tracker_number(url) == number


def _url_accepts(url: str, number: str) -> bool:
    """False for a homepage, a bare /app index, or a different app id."""
    parsed = urlparse(url or "")
    path = (parsed.path or "/").rstrip("/") or "/"
    host = (parsed.netloc or "").lower()
    if "appstoretracker.com" in host and path in {"/", "/app"}:
        return False
    found = _tracker_number(url)
    if found and found != number:
        return False
    if "appstoretracker.com" in host and "/app/" in path and not found:
        return False
    return True


def _primary_identity_urls(body: str) -> list[str]:
    urls = []
    for pattern in (_CANONICAL, _OG_URL):
        match = pattern.search(body or "")
        if not match:
            continue
        href = next((group for group in match.groups() if group), "")
        if href:
            urls.append(href)
    return urls


def _is_missing_page(body: str) -> bool:
    match = _TITLE.search(body or "")
    title = re.sub(r"\s+", " ", match.group(1) if match else "").strip().lower()
    return "404" in title or "page not found" in title or title == "not found"


def _title_confirms(body: str, number: str, slug: str) -> bool:
    chunks = []
    title = _TITLE.search(body or "")
    if title:
        chunks.append(re.sub(r"\s+", " ", title.group(1)))
    og = _OG_TITLE.search(body or "")
    if og:
        chunks.append(next((group for group in og.groups() if group), ""))
    blob = " ".join(chunks)
    if number and number in blob:
        return True
    if slug and slug in blob.lower().replace(" ", "-"):
        return True
    return False


def _is_metric_container(node) -> bool:
    classes = node.get("class") or []
    if not any(str(item).split("/")[0] == "@container" for item in classes):
        return False
    return node.find(["dt", "th"]) is not None


def _metric_containers(soup) -> list:
    """Rank, rating, revenue, and downloads, in that order."""
    tiles = []
    for node in soup.find_all(True):
        if not _is_metric_container(node):
            continue
        if any(_is_metric_container(child) for child in node.find_all(True)):
            continue
        tiles.append(node)
    return tiles


def _is_paywall_decoy(node) -> bool:
    """The logged-out revenue tile is blurred and not a real estimate."""
    if str(node.get("aria-hidden") or "").lower() == "true":
        return True
    for item in (node, *node.find_all(True)):
        classes = " ".join(item.get("class") or [])
        if "select-none" in classes or re.search(r"(?:^|\s)blur-", classes):
            return True
    return False


def _revenue_tile(node) -> tuple[str, int | None]:
    if _is_paywall_decoy(node):
        return "", None
    label = node.find(["dt", "th"])
    label_text = label.get_text(" ", strip=True) if label is not None else ""
    if not re.search(r"\brevenue\b", label_text, re.IGNORECASE):
        return "", None
    cells = [item.get_text(" ", strip=True) for item in node.find_all(["dd", "td"])]
    primary = cells[0] if cells else ""
    if _UNDER_5K.match(primary):
        return "< $5k MRR", None
    if _is_empty_marker(primary):
        return "", None
    amounts = list(_MONEY.finditer(primary))
    if len(amounts) != 1:
        return "", None
    block = " ".join(cell for cell in cells if cell)
    if not _MONTHLY.search(primary) and not _MONTHLY.search(block):
        return "", None
    amount = _amount_from_match(amounts[0])
    if amount is None or amount <= 0:
        return "", None
    return _pack(amount)


def _labeled_amount(soup) -> tuple[str, int | None] | None:
    """The first Revenue/MRR field, or None when the page has no such field.

    An empty primary cell stops the parse. A figure elsewhere on the page,
    including a similar-app block, is not this app's revenue.
    """
    for label in soup.find_all(["dt", "th"]):
        text = label.get_text(" ", strip=True)
        if not _LABEL.match(text):
            continue
        primary, block = _value_for_label(label)
        return _resolve_labeled_value(primary, block)
    return None


def _value_for_label(label) -> tuple[str, str]:
    cells: list[str] = []
    parent = label.parent
    if parent is not None:
        cells = [node.get_text(" ", strip=True) for node in parent.find_all("dd")]
    if not cells:
        sibling = label.find_next_sibling()
        while sibling is not None and getattr(sibling, "name", None) in {"dd", "span", "p"}:
            cells.append(sibling.get_text(" ", strip=True))
            sibling = sibling.find_next_sibling()
    primary = cells[0] if cells else ""
    block = " ".join(cell for cell in cells if cell)
    return primary, block


def _resolve_labeled_value(primary: str, block: str) -> tuple[str, int | None]:
    if _is_empty_marker(primary):
        return "", None
    amounts = list(_MONEY.finditer(primary or ""))
    if len(amounts) != 1:
        return "", None
    if not _MONTHLY.search(primary or "") and not _MONTHLY.search(block or ""):
        return "", None
    amount = _amount_from_match(amounts[0])
    if amount is None or amount <= 0:
        return "", None
    return _pack(amount)


def _inline_labeled_amount(soup) -> tuple[str, int | None]:
    """A single element that itself says MRR or Revenue and contains one monthly amount."""
    found: list[int] = []
    for node in soup.find_all(["p", "li", "span", "div", "td", "dd", "h1", "h2", "h3"]):
        text = node.get_text(" ", strip=True)
        if not text or len(text) > 400:
            continue
        if not re.search(r"\b(?:mrr|revenue)\b", text, re.IGNORECASE):
            continue
        if not _MONTHLY.search(text):
            continue
        amounts = list(_MONEY.finditer(text))
        if len(amounts) != 1:
            continue
        amount = _amount_from_match(amounts[0])
        if amount is None or amount <= 0:
            continue
        found.append(int(round(amount)))
    distinct = set(found)
    if len(distinct) != 1:
        return "", None
    return _pack(next(iter(distinct)))


def _pack(amount: float) -> tuple[str, int | None]:
    numeric = int(round(amount))
    if numeric <= 0:
        return "", None
    return bucket_mrr_amount(numeric), numeric


def _is_empty_marker(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    compact = raw.lower().replace(" ", "")
    if compact in _EMPTY_TOKENS:
        return True
    return re.fullmatch(r"\$?0+(?:\.0+)?(?:k|m)?(?:/mo(?:nth)?)?", compact) is not None


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
