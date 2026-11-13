"""Find public emails on a developer website. Never invent an address."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from modules.models import EmailFound, Lead

LOGGER = logging.getLogger("prospecting.enrichment")

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
OBFUSCATED_RE = re.compile(
    r"([A-Z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\sat\s)\s*([A-Z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*([A-Z]{2,})",
    re.IGNORECASE,
)
# Homepage, contact, and support stay inside the 3-page cap. Privacy follows when a slot remains.
FAST_PATHS = (
    "/contact",
    "/support",
    "/privacy",
    "/privacy-policy",
)
CONTACT_PATHS = FAST_PATHS
HEAVY_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".mp4", ".mov", ".webm", ".zip", ".gz", ".css", ".js",
    ".woff", ".woff2", ".mp3",
)
CRAWL_WORKERS = 12
PAGE_CAP = 3
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 5.0
LINK_HINTS = ("contact", "privacy", "terms", "imprint", "legal", "about", "support")
JUNK_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "email.com",
    "sentry.io",
    "wix.com",
    "wixpress.com",
    "wixsite.com",
    "apple.com",
    "schema.org",
    "godaddy.com",
    "cloudflare.com",
    "mysite.com",
    "yourdomain.com",
    "company.com",
    "doe.com",
}
JUNK_LOCALS = {"user", "username", "name", "email", "yourname", "youremail", "example", "test", "emailaddress"}
SKIP_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply"}
SUPPORT_LOCALS = {"support", "help", "customerservice", "customer", "feedback", "care", "service"}
GENERAL_LOCALS = {
    "info", "contact", "hello", "hi", "team", "office", "mail",
    "enquiries", "inquiries", "admin", "sales", "founder",
}
_IMAGE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}
FOUNDER_WORDS = (
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "built by",
    "created by",
    "made by",
    "indie developer",
)
NAME_RE = r"([A-Z][a-z]+(?:[ '\-][A-Z][a-z]+){0,2})"
_ROLE = r"(?i:founder|co-founder|cofounder|ceo|creator|maker)"
CONTACT_NAME_PATTERNS = (
    re.compile(rf"{_ROLE}\s*[:\-\u2013]\s*{NAME_RE}"),
    re.compile(rf"{NAME_RE}\s*,\s*{_ROLE}\b"),
)


class SiteBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.strip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return bool(literal.is_global)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            return False
    return bool(addresses) and all(ip.is_global for ip in addresses)


def normalize_website(url: str | None) -> str:
    if not url:
        return ""
    text = url.strip()
    if not text or text.lower() in {"n/a", "none", "null"}:
        return ""
    if text.lower().startswith("mailto:"):
        return ""
    if not re.match(r"https?://", text, re.IGNORECASE):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.hostname:
        return ""
    return parsed._replace(fragment="").geturl()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_site(url: str, origin: str) -> bool:
    parsed = urlparse(url)
    root = urlparse(origin)
    host = (parsed.hostname or "").lower()
    root_host = (root.hostname or "").lower()
    return host == root_host or host.endswith("." + root_host)


def _junk_domain(domain: str) -> bool:
    host = domain.lower().strip(".")
    for junk in JUNK_DOMAINS:
        if host == junk or host.endswith("." + junk):
            return True
    return False


def _skipped_local(local: str) -> bool:
    base = local.split("+", 1)[0].lower()
    if base in SKIP_LOCALS or local.lower() in SKIP_LOCALS:
        return True
    compact = re.sub(r"[._\-]", "", base)
    return compact in {"noreply", "donotreply", "noresponse"}


def _clean_email(value: str) -> str | None:
    email = unquote(value).strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _IMAGE_TLDS:
        return None
    if _junk_domain(domain) or local in JUNK_LOCALS or _skipped_local(local):
        return None
    if not re.search(r"[a-z]", local):
        return None
    return email


def _is_blocked_address(value: str) -> bool:
    """A real mailbox we refuse (noreply, a store, or a site builder). Image false-positives are ignored."""
    email = unquote(value or "").strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return False
    local, _, domain = email.partition("@")
    if domain.rsplit(".", 1)[-1] in _IMAGE_TLDS:
        return False
    return _junk_domain(domain) or local in JUNK_LOCALS or _skipped_local(local)


def decode_cloudflare_email(hex_string: str) -> str | None:
    try:
        data = bytes.fromhex(hex_string)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    decoded = "".join(chr(byte ^ key) for byte in data[1:])
    return _clean_email(decoded)


def _scan_emails(html: str) -> tuple[list[str], int]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    rejected = 0
    seen: set[str] = set()

    def add(value: str | None) -> None:
        nonlocal rejected
        raw = unquote(value or "").strip()
        if not raw:
            return
        key = raw.lower()
        if key in seen:
            return
        seen.add(key)
        cleaned = _clean_email(raw)
        if cleaned and cleaned not in found:
            found.append(cleaned)
        elif _is_blocked_address(raw):
            rejected += 1

    for node in soup.select("[data-cfemail]"):
        add(decode_cloudflare_email(node.get("data-cfemail") or ""))
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        if href.lower().startswith("mailto:"):
            add(href.split(":", 1)[1].split("?", 1)[0])
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    for match in EMAIL_RE.findall(text):
        add(match)
    for local, domain, tld in OBFUSCATED_RE.findall(text):
        add(f"{local}@{domain}.{tld}")
    return found, rejected


def extract_emails(html: str) -> list[str]:
    kept, _rejected = _scan_emails(html)
    return kept


def split_store_link(value: str | None) -> tuple[str, str | None]:
    """Split a store URL into an http(s) site and a mailto address. Never fetch a mailto."""
    if value is None:
        return "", None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "none", "null"}:
        return "", None
    if text.lower().startswith("mailto:"):
        address = _clean_email(text.split(":", 1)[1].split("?", 1)[0])
        return "", address
    return normalize_website(text), None


def apply_store_links(lead: Lead, seller_raw, support_raw=None) -> None:
    """Keep seller and support links. A mailto becomes a lead email instead of a fake https URL."""
    website, seller_email = split_store_link(seller_raw)
    support, support_email = split_store_link(support_raw)
    if not lead.developer_website and website:
        lead.developer_website = website
    if support and not lead.developer_website:
        lead.developer_website = support
        support = ""
    if support and lead.developer_website and _origin(support) != _origin(lead.developer_website):
        if not lead.support_url:
            lead.support_url = support
    known = {item.address.lower() for item in lead.emails}
    for address in (seller_email, support_email):
        if not address or address in known:
            continue
        known.add(address)
        email_type, evidence = classify_email(address, "", lead.developer_name, lead.developer_type or "unknown")
        lead.emails.append(EmailFound(address, email_type, "App Store listing", evidence))


def _evidence_window(text: str, email: str) -> str:
    haystack = text.lower()
    needle = email.lower()
    index = haystack.find(needle)
    if index < 0:
        return ""
    start = max(0, index - 160)
    end = min(len(text), index + len(email) + 160)
    return text[start:end]


def _person_tokens(name: str) -> list[str]:
    return [part.lower() for part in re.findall(r"[A-Za-z]{2,}", name)]


def classify_email(address: str, page_text: str, developer_name: str, developer_type: str) -> tuple[str, str]:
    local = address.split("@", 1)[0]
    if local.split("+", 1)[0].split(".")[0] in SUPPORT_LOCALS or local in SUPPORT_LOCALS:
        return "support", "The address uses a support mailbox name."
    window = _evidence_window(page_text, address).lower()
    founder_hit = next((word for word in FOUNDER_WORDS if word in window), "")
    generic = local.split(".")[0] in GENERAL_LOCALS or local in GENERAL_LOCALS or local.split("+", 1)[0] in GENERAL_LOCALS
    if founder_hit and not generic and local.split(".")[0] not in SUPPORT_LOCALS:
        return "founder", f"The page text around the address says {founder_hit!r}."
    if developer_type == "solo" and _mailbox_matches_person(address, developer_name) and not generic:
        return "founder", "The mailbox matches the solo developer's name."
    if generic or local in GENERAL_LOCALS:
        return "general", "The address uses a general business mailbox name."
    return "general", "Public business address with no founder evidence."


def _mailbox_matches_person(address: str, developer_name: str) -> bool:
    tokens = _person_tokens(developer_name)
    if len(tokens) < 2:
        return False
    local = address.split("@", 1)[0].lower()
    local_key = local.replace(".", "").replace("_", "").replace("-", "")
    joined = "".join(tokens)
    first_last = tokens[0] + tokens[-1]
    dotted = f"{tokens[0]}.{tokens[-1]}"
    return local_key in {joined, first_last} or local in {dotted, ".".join(tokens)}


def extract_contact_name(text: str) -> str:
    for pattern in CONTACT_NAME_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1).strip()
    return ""


def _robots_for(origin: str, fetcher, user_agent: str, public_check=is_public_url) -> RobotFileParser | None:
    robots_url = origin.rstrip("/") + "/robots.txt"
    if not public_check(robots_url):
        raise SiteBlocked("the host is not a public address")
    try:
        status, body, _final = fetcher(robots_url)
    except SiteBlocked:
        raise
    except Exception as exc:
        LOGGER.warning("Could not read %s: %s", robots_url, exc)
        return None
    if status >= 400 or body is None:
        LOGGER.info("No robots.txt at %s (status %s). Continuing with a small page set.", robots_url, status)
        return None
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    parser.set_url(robots_url)
    # Touch the user agent so a misconfigured parser still has one.
    parser.can_fetch(user_agent, origin + "/")
    return parser


def _allowed(parser: RobotFileParser | None, user_agent: str, url: str) -> bool:
    if parser is None:
        return True
    try:
        return bool(parser.can_fetch(user_agent, url))
    except Exception:
        LOGGER.warning("Could not apply robots.txt to %s. Skipping that URL.", url)
        return False


def _candidate_links(html: str, page_url: str, origin: str, limit: int) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    found = []
    for link in soup.select("a[href]"):
        href = (link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        if not _same_site(absolute, origin):
            continue
        label = f"{href} {(link.get_text(' ', strip=True) or '')}".lower()
        if not any(hint in label for hint in LINK_HINTS):
            continue
        cleaned = absolute.split("#", 1)[0]
        if cleaned not in found:
            found.append(cleaned)
        if len(found) >= limit:
            break
    return found


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _is_heavy_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(HEAVY_EXTENSIONS)


def _page_list(start_url: str) -> list[str]:
    origin = _origin(start_url)
    pages = [start_url]
    for path in FAST_PATHS:
        pages.append(origin.rstrip("/") + path)
    ordered = []
    seen = set()
    for page in pages:
        if page not in seen:
            seen.add(page)
            ordered.append(page)
    return ordered


def _crawl_origin(
    lead: Lead,
    start_url: str,
    *,
    fetch,
    user_agent: str,
    public_check,
    sleep,
    delay: float,
    max_pages: int,
    emails: list[EmailFound],
    known: set[str],
) -> tuple[bool, int, list[str], str, str]:
    """Fetch at most `max_pages` HTML documents on one host. A blocked page does not drop the lead."""
    origin = _origin(start_url)
    try:
        robots = _robots_for(origin, fetch, user_agent, public_check)
    except SiteBlocked as exc:
        return False, 0, [str(exc)], "", lead.contact_name
    except Exception as exc:
        LOGGER.warning("robots.txt check failed for %s: %s", origin, exc)
        robots = None

    ordered = _page_list(start_url)
    fetched_any = False
    blocked_pages = 0
    errors: list[str] = []
    contact_page = ""
    contact_name = lead.contact_name
    index = 0
    while index < len(ordered) and index < max_pages and not emails:
        page = ordered[index]
        index += 1
        if _is_heavy_url(page):
            continue
        if not _allowed(robots, user_agent, page):
            blocked_pages += 1
            LOGGER.info("Skipped %s because robots.txt disallows it.", page)
            continue
        if delay and index > 1:
            sleep(delay)
        try:
            status, body, final_url = fetch(page)
        except SiteBlocked as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            errors.append(f"{page}: {exc}")
            LOGGER.warning("Could not fetch %s: %s", page, exc)
            continue
        if status in {401, 403, 429}:
            errors.append(f"{final_url or page} returned {status}.")
            continue
        if status >= 400 or not body:
            continue
        fetched_any = True
        if any(part in (final_url or page) for part in ("/contact", "/support", "/about", "/imprint")):
            contact_page = final_url or page
        text = _visible_text(body)
        if not contact_name:
            contact_name = extract_contact_name(text)
        kept, rejected = _scan_emails(body)
        lead.rejected_email_count = int(lead.rejected_email_count or 0) + rejected
        for address in kept:
            if address in known:
                continue
            known.add(address)
            email_type, evidence = classify_email(address, text, lead.developer_name, lead.developer_type)
            emails.append(EmailFound(address, email_type, final_url or page, evidence))
    return fetched_any, blocked_pages, errors, contact_page, contact_name


def enrich_lead(lead: Lead, settings: dict, *, fetcher=None, sleep=time.sleep, public_check=is_public_url) -> Lead:
    """Crawl a few public pages on the seller site and, when needed, the support site."""
    config = settings.get("enrichment") or {}
    user_agent = config.get("user_agent") or "AppProspectingCLI/1.0"
    website = normalize_website(lead.developer_website)
    support = normalize_website(getattr(lead, "support_url", "") or "")
    lead.developer_website = website
    if support and website and _origin(support) == _origin(website):
        support = ""
    lead.support_url = support
    if not website and not support:
        if lead.emails:
            lead.enrichment_status = "ok"
            lead.enrichment_note = "Email came from the store listing."
            _finalize_contacts(lead)
            return lead
        lead.enrichment_status = "no_website"
        lead.enrichment_note = "No developer website."
        return lead
    if website and not public_check(website):
        lead.enrichment_status = "blocked"
        lead.enrichment_note = "Developer website is not a public address."
        return lead
    if support and not public_check(support):
        support = ""
        lead.support_url = ""

    fetch = fetcher or _build_fetcher(config)
    delay = float(config.get("delay_seconds") or 0)
    max_pages = min(PAGE_CAP, int(config.get("max_pages_per_site") or PAGE_CAP))
    emails: list[EmailFound] = list(lead.emails)
    known = {item.address.lower() for item in emails}
    contact_page = ""
    fetched_any = False
    blocked_pages = 0
    errors: list[str] = []
    for start in [url for url in (website, support) if url]:
        if emails:
            break
        got_page, blocked, page_errors, found_contact, contact_name = _crawl_origin(
            lead,
            start,
            fetch=fetch,
            user_agent=user_agent,
            public_check=public_check,
            sleep=sleep,
            delay=delay,
            max_pages=max_pages,
            emails=emails,
            known=known,
        )
        fetched_any = fetched_any or got_page
        blocked_pages += blocked
        errors.extend(page_errors)
        lead.contact_name = contact_name or lead.contact_name
        if found_contact:
            contact_page = found_contact

    lead.emails = emails
    lead.contact_page_url = contact_page or (emails[0].source_url if emails else lead.contact_page_url)
    _finalize_contacts(lead)
    if emails:
        lead.enrichment_status = "ok"
        lead.enrichment_note = f"Found {len(emails)} public email(s)."
    elif fetched_any and lead.rejected_email_count:
        lead.enrichment_status = "no_email"
        lead.enrichment_note = "Only automated or junk addresses were listed."
    elif fetched_any:
        lead.enrichment_status = "no_email"
        lead.enrichment_note = "Pages were fetched and no public email was listed."
    elif blocked_pages and not errors:
        lead.enrichment_status = "blocked"
        lead.enrichment_note = "robots.txt disallows the pages we would have checked."
    else:
        lead.enrichment_status = "error"
        lead.enrichment_note = "; ".join(errors[:3]) or "The website could not be fetched."
    return lead


def crawl_leads(leads: list, enrich, *, workers: int = CRAWL_WORKERS) -> None:
    """Enrich many leads at once. One slow host occupies a single worker, not the whole run."""
    if not leads:
        return
    if len(leads) == 1:
        enrich(leads[0])
        return
    worker_count = max(1, min(int(workers), len(leads)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="site-crawl") as pool:
        futures = [pool.submit(enrich, lead) for lead in leads]
        for future in futures:
            future.result()


def _finalize_contacts(lead: Lead) -> None:
    primary = lead.primary_email
    if primary and not lead.contact_page_url:
        lead.contact_page_url = primary.source_url


def _build_fetcher(config: dict):
    session = requests.Session()
    # Connect 3s and read 5s so one stalled server cannot hold a crawl worker.
    timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)
    max_bytes = int(config.get("max_bytes") or 1_000_000)
    user_agent = config.get("user_agent") or "AppProspectingCLI/1.0"

    def fetch(url: str) -> tuple[int, str | None, str]:
        current = url
        for _hop in range(5):
            if _is_heavy_url(current):
                return 204, None, current
            if not is_public_url(current):
                raise SiteBlocked(f"{current} is not a public http(s) address")
            response = session.get(
                current,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8"},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    return response.status_code, None, current
                current = urljoin(current, location)
                continue
            content_type = response.headers.get("Content-Type", "")
            if response.status_code >= 400:
                response.close()
                return response.status_code, None, current
            if content_type and "html" not in content_type and "text/plain" not in content_type:
                response.close()
                return response.status_code, None, current
            raw = response.raw.read(max_bytes + 1, decode_content=True)
            response.close()
            if isinstance(raw, str):
                text = raw[:max_bytes]
            else:
                text = raw[:max_bytes].decode(response.encoding or "utf-8", errors="replace")
            return response.status_code, text, current
        raise SiteBlocked(f"{url} redirected too many times")

    return fetch
