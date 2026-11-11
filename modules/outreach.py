"""Template rendering and the separate, explicitly confirmed Gmail send."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from modules.config import project_path, save_json
from modules.errors import ProspectingError
from modules.models import Lead

LOGGER = logging.getLogger("prospecting.outreach")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


class OutreachError(ProspectingError):
    pass


def render_template(template: str, lead: Lead) -> str:
    name = (lead.contact_name or "").strip()
    category = lead.category or "app"
    rendered = (
        template.replace("{{App Name}}", lead.app_name or "your app")
        .replace("{{Contact Name}}", name)
        .replace("{{Category}}", category)
    )
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    rendered = rendered.replace("Hi ,", "Hi,").replace("Hi  ,", "Hi,")
    return rendered.strip()


def load_suppression(settings: dict) -> list[dict]:
    path = project_path(settings.get("suppression_file", "data/suppression.json"))
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutreachError(f"Could not read the suppression list at {path}: {exc}") from exc
    addresses = data.get("addresses") if isinstance(data, dict) else None
    if not isinstance(addresses, list):
        raise OutreachError(f"{path} must be an object with an addresses list.")
    return addresses


def suppressed_addresses(settings: dict) -> set[str]:
    return {
        str(item.get("email", "")).strip().lower()
        for item in load_suppression(settings)
        if isinstance(item, dict) and item.get("email")
    }


def add_suppression(settings: dict, email: str, reason: str = "") -> Path:
    cleaned = email.strip().lower()
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise OutreachError(f"{email!r} is not an email address.")
    path = project_path(settings.get("suppression_file", "data/suppression.json"))
    current = load_suppression(settings) if path.exists() else []
    if any(str(item.get("email", "")).lower() == cleaned for item in current):
        return path
    current.append(
        {
            "email": cleaned,
            "reason": reason.strip(),
            "added_at": datetime.now(timezone.utc).date().isoformat(),
        }
    )
    save_json(path, {"addresses": current})
    return path


def remove_suppression(settings: dict, email: str) -> bool:
    path = project_path(settings.get("suppression_file", "data/suppression.json"))
    current = load_suppression(settings) if path.exists() else []
    cleaned = email.strip().lower()
    kept = [item for item in current if str(item.get("email", "")).lower() != cleaned]
    if len(kept) == len(current):
        return False
    save_json(path, {"addresses": kept})
    return True


def sendable_rows(records: list[dict], settings: dict, suppressed: set[str]) -> tuple[list[dict], list[str]]:
    allowed = {status.lower() for status in settings.get("outreach", {}).get("sendable_statuses", ["New"])}
    today = datetime.now(timezone.utc).date().isoformat()
    sent_today = sum(1 for record in records if str(record.get("sent_date", "")).startswith(today))
    limit = int(settings.get("outreach", {}).get("daily_limit", 20))
    remaining = max(0, limit - sent_today)
    ready = []
    notes = [f"Sent today: {sent_today}. Daily limit: {limit}. Remaining: {remaining}."]
    if remaining == 0:
        notes.append("The daily limit has already been reached. Nothing will be sent.")
        return [], notes
    for record in records:
        email = str(record.get("email") or "").strip()
        status = str(record.get("status") or "")
        if status.lower() not in allowed:
            continue
        if record.get("sent_date"):
            continue
        if not email:
            continue
        if email.lower() in suppressed:
            notes.append(f"Skipped {email} because it is on the suppression list.")
            continue
        ready.append(record)
        if len(ready) >= remaining:
            break
    if len(ready) < remaining:
        pass
    else:
        notes.append("The candidate list was cut at the daily limit.")
    return ready, notes


def send_messages(service, rows: list[dict], signature: str = "") -> list[dict]:
    """Send without artificial delays. Returns one result per row."""
    from email.message import EmailMessage
    import base64

    results = []
    for row in rows:
        body = (row.get("body") or "").strip()
        if signature and signature.strip() and signature.strip() not in body:
            body = body + "\n\n" + signature.strip()
        message = EmailMessage()
        message["To"] = row["email"]
        message["Subject"] = row.get("subject") or "(no subject)"
        message.set_content(body or "")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as exc:
            LOGGER.error("Gmail send failed for row %s: %s", row.get("row_number"), exc)
            results.append({"row": row, "ok": False, "error": str(exc)})
            continue
        results.append({"row": row, "ok": True, "id": sent.get("id", "")})
    return results


def gmail_service(settings: dict):
    client_path = project_path(settings.get("outreach", {}).get("gmail_client_file", "credentials/gmail-oauth.json"))
    token_path = project_path(settings.get("outreach", {}).get("gmail_token_file", "credentials/gmail-token.json"))
    if not client_path.exists():
        raise OutreachError(
            "Gmail is not configured. Download an OAuth client JSON for a Desktop app and save it at "
            f"{client_path}. Sending was not attempted. See README.md."
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise OutreachError("Gmail libraries are missing. Run: pip install -r requirements.txt") from exc
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_path), GMAIL_SCOPES)
            credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials)


def primary_signature(service) -> str:
    try:
        from bs4 import BeautifulSoup

        listed = service.users().settings().sendAs().list(userId="me").execute()
    except Exception as exc:
        LOGGER.warning("Could not read the Gmail signature: %s", exc)
        print(f"Could not read the Gmail signature ({exc}). Messages will be sent without it.")
        return ""
    for item in listed.get("sendAs") or []:
        if item.get("isPrimary"):
            html = item.get("signature") or ""
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return ""


def record_sends(crm, results: list[dict], sent_on: date | None = None) -> None:
    when = sent_on or datetime.now(timezone.utc).date()
    for result in results:
        if not result.get("ok"):
            print(f"Not sent to {result['row'].get('email')}: {result.get('error')}")
            continue
        row = result["row"]
        crm.mark_sent(row["row_number"], row.get("url") or "", when)
        print(f"Sent to {row.get('email')} and logged row {row['row_number']} (message {result.get('id')}).")
