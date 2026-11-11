"""Find public emails on a developer website. Never invent an address."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlparse
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
CONTACT_PATHS = (
    "/contact",
    "/contact-us",
    "/privacy",
    "/privacy-policy",
    "/privacy_policy",
    "/terms",
    "/terms-of-service",
    "/legal",
    "/imprint",
    "/about",
)
LINK_HINTS = ("contact", "privacy", "terms", "imprint", "legal", "about", "support")
JUNK_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
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
GENERAL_LOCALS = {"info", "contact", "hello", "hi", "team", "office", "mail", "enquiries", "inquiries", "admin", "sales"}
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


def _clean_email(value: str) -> str | None:
    email = value.strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}:
        return None
    if domain in JUNK_DOMAINS or local in JUNK_LOCALS or local in SKIP_LOCALS:
        return None
    if not re.search(r"[a-z]", local):
        return None
    return email


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


def extract_emails(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []

    def add(value: str | None) -> None:
        cleaned = _clean_email(value or "")
        if cleaned and cleaned not in found:
            found.append(cleaned)

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
    return found


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


def enrich_lead(lead: Lead, settings: dict, *, fetcher=None, sleep=time.sleep, public_check=is_public_url) -> Lead:
    """Crawl a few public pages. A failure is recorded on the lead and does not raise."""
    config = settings.get("enrichment") or {}
    user_agent = config.get("user_agent") or "AppProspectingCLI/1.0"
    website = normalize_website(lead.developer_website)
    lead.developer_website = website
    if not website:
        if lead.emails:
            lead.enrichment_status = "ok"
            lead.enrichment_note = "Email came from the store listing."
            _finalize_contacts(lead)
            return lead
        lead.enrichment_status = "no_website"
        lead.enrichment_note = "No developer website."
        return lead
    if not public_check(website):
        lead.enrichment_status = "blocked"
        lead.enrichment_note = "Developer website is not a public address."
        return lead

    fetch = fetcher or _build_fetcher(config)
    origin = _origin(website)
    try:
        robots = _robots_for(origin, fetch, user_agent, public_check)
    except SiteBlocked as exc:
        lead.enrichment_status = "blocked"
        lead.enrichment_note = str(exc)
        return lead
    except Exception as exc:
        LOGGER.warning("robots.txt check failed for %s: %s", origin, exc)
        robots = None

    pages = [website]
    for path in CONTACT_PATHS:
        pages.append(origin.rstrip("/") + path)
    seen = set()
    ordered = []
    for page in pages:
        if page not in seen:
            seen.add(page)
            ordered.append(page)

    delay = float(config.get("delay_seconds") or 0)
    max_pages = int(config.get("max_pages_per_site") or 7)
    extra_limit = int(config.get("max_extra_links") or 3)
    emails: list[EmailFound] = list(lead.emails)
    known = {item.address.lower() for item in emails}
    contact_page = ""
    contact_name = lead.contact_name
    fetched_any = False
    blocked_pages = 0
    errors = []

    index = 0
    while index < len(ordered) and index < max_pages:
        page = ordered[index]
        index += 1
        if not _allowed(robots, user_agent, page):
            blocked_pages += 1
            LOGGER.info("Skipped %s because robots.txt disallows it.", page)
            continue
        if delay and index > 1:
            sleep(delay)
        try:
            status, body, final_url = fetch(page)
        except SiteBlocked as exc:
            lead.enrichment_status = "blocked"
            lead.enrichment_note = str(exc)
            return lead
        except Exception as exc:
            errors.append(f"{page}: {exc}")
            LOGGER.warning("Could not fetch %s: %s", page, exc)
            continue
        if status in {401, 403, 429}:
            lead.enrichment_status = "blocked"
            lead.enrichment_note = f"{final_url or page} returned {status}."
            return lead
        if status >= 400 or not body:
            continue
        fetched_any = True
        if any(part in (final_url or page) for part in ("/contact", "/about", "/imprint")):
            contact_page = final_url or page
        text = _visible_text(body)
        if not contact_name:
            contact_name = extract_contact_name(text)
        for address in extract_emails(body):
            if address in known:
                continue
            known.add(address)
            email_type, evidence = classify_email(address, text, lead.developer_name, lead.developer_type)
            emails.append(EmailFound(address, email_type, final_url or page, evidence))
        if index == 1:
            for link in _candidate_links(body, final_url or page, origin, extra_limit):
                if link not in seen:
                    seen.add(link)
                    ordered.append(link)

    lead.emails = emails
    lead.contact_name = contact_name
    lead.contact_page_url = contact_page or (emails[0].source_url if emails else "")
    _finalize_contacts(lead)
    if emails:
        lead.enrichment_status = "ok"
        lead.enrichment_note = f"Found {len(emails)} public email(s)."
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


def _finalize_contacts(lead: Lead) -> None:
    primary = lead.primary_email
    if primary and not lead.contact_page_url:
        lead.contact_page_url = primary.source_url


def _build_fetcher(config: dict):
    session = requests.Session()
    timeout = float(config.get("timeout_seconds") or 12)
    max_bytes = int(config.get("max_bytes") or 1_000_000)
    user_agent = config.get("user_agent") or "AppProspectingCLI/1.0"

    def fetch(url: str) -> tuple[int, str | None, str]:
        current = url
        for _hop in range(5):
            if not is_public_url(current):
                raise SiteBlocked(f"{current} is not a public http(s) address")
            response = session.get(
                current,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
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
