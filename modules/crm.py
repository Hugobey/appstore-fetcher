"""Google Sheets CRM. Existing rows are never overwritten or deleted."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from modules.config import project_path
from modules.deduplication import DedupIndex, canonical_url, identity_from_url
from modules.errors import CRMError
from modules.labels import TEAM_LABELS
from modules.models import Lead
from modules.outreach import render_template
from modules.sheets_crm import CRM_COLUMNS, apply_crm_layout

LOGGER = logging.getLogger("prospecting.crm")

BASE_HEADERS = [
    "App Name",
    "Developer",
    "Target Email",
    "Contact Name",
    "App Store URL",
    "Last Update Date",
    "Status",
    "Email Subject",
    "Generated Body",
    "Sent Date",
]
EXTRA_HEADERS = [
    "Lead Score",
    "Email Source",
    "Email Type",
    "Platform",
    "Ratings",
    "Review Count",
    "Category",
    "App Store ID",
    "Developer Website",
    "Notes",
]
# A blank worksheet gets this row. Generated Body and Email Subject are absent.
BLANK_HEADERS = list(CRM_COLUMNS)
STATUS_NEW = "New"
STATUS_TO_CONTACT = "To Contact"
STATUS_MISSING = "Missing email"
STATUS_SUPPRESSED = "Suppressed"
STATUS_SENT = "Sent"
_LAYOUT_COLUMNS = [
    "Contact Email",
    "Follow-Up Date",
    "Last Contacted",
    "Estimated MRR",
]


def column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    # An empty string is a substring of every string, so test the first character.
    if text and text[0] in "=+-@":
        return "'" + text
    return text


def find_header_row(values: list[list[str]]) -> int | None:
    """Return a 0-based row index that contains the CRM headers."""
    for index, row in enumerate(values[:10]):
        normalized = {str(cell).strip().lower() for cell in row}
        if "app name" in normalized and "app store url" in normalized:
            return index
    return None


def headers_to_add(existing: list[str]) -> list[str]:
    present = {str(cell).strip().lower() for cell in existing if str(cell).strip()}
    wanted = list(EXTRA_HEADERS) + list(_LAYOUT_COLUMNS)
    return [header for header in wanted if header.lower() not in present]


def row_for_lead(lead: Lead, headers: list[str], subject: str, body: str, status: str) -> list[str]:
    primary = lead.primary_email
    others = [item.address for item in lead.emails if not primary or item.address != primary.address]
    notes = _notes(lead, others)
    values = {
        "App Name": lead.app_name,
        "Developer": lead.developer_name,
        "Target Email": primary.address if primary else "",
        "Contact Email": primary.address if primary else "",
        "Contact Name": lead.contact_name,
        "App Store URL": lead.app_store_url,
        "Last Update Date": lead.last_update or "",
        "Status": status,
        "Follow-Up Date": "",
        "Last Contacted": "",
        "Email Subject": subject,
        "Generated Body": body,
        "Sent Date": "",
        "Lead Score": "" if lead.score_normalized is None else lead.score_normalized,
        "Estimated MRR": lead.estimated_mrr or "",
        "Email Source": primary.source_url if primary else "",
        "Email Type": _email_type_label(primary.email_type) if primary else "",
        "Platform": "iOS" if lead.platform == "ios" else "Android",
        "Ratings": "" if lead.rating_count is None else lead.rating_count,
        "Review Count": "" if lead.review_count is None else lead.review_count,
        "Category": lead.category,
        "App Store ID": lead.app_store_id,
        "Developer Website": lead.developer_website,
        "Notes": notes,
    }
    return [safe_cell(values.get(header, "")) for header in headers]


def _email_type_label(value: str) -> str:
    return {"founder": "Founder", "general": "General", "support": "Support"}.get(value, value)


def _notes(lead: Lead, other_emails: list[str]) -> str:
    parts = []
    icp = TEAM_LABELS.get(lead.developer_type)
    if icp:
        evidence = f" {lead.icp_reason}" if lead.icp_reason else ""
        parts.append(f"ICP: {icp}.{evidence}")
    if lead.score_normalized is not None:
        parts.append(
            f"Score {lead.score_normalized}/100 from {lead.score_points}/{lead.score_known_max} known points."
        )
    if lead.score_missing:
        parts.append("Missing: " + ", ".join(lead.score_missing) + ".")
    awarded = [item["detail"] for item in lead.score_breakdown if item.get("status") == "awarded"]
    if awarded:
        parts.append(" ".join(awarded))
    if other_emails:
        parts.append("Other public emails: " + ", ".join(other_emails) + ".")
    if lead.enrichment_note:
        parts.append(lead.enrichment_note)
    return " ".join(parts)[:1500]


def import_status(lead: Lead, suppressed: set[str]) -> tuple[str, str, str]:
    primary = lead.primary_email
    if primary and primary.address.lower() in suppressed:
        return STATUS_SUPPRESSED, "", ""
    return (STATUS_TO_CONTACT if primary else STATUS_MISSING), "", ""


def build_import_row(lead: Lead, headers: list[str], template: dict | None, suppressed: set[str]) -> list[str]:
    status, _subject, _body = import_status(lead, suppressed)
    subject = ""
    body = ""
    if status not in {STATUS_MISSING, STATUS_SUPPRESSED} and template is not None:
        subject = render_template(template.get("subject", ""), lead)
        body = render_template(template.get("body", ""), lead)
    return row_for_lead(lead, headers, subject, body, status)


class GoogleSheetCRM:
    def __init__(self, spreadsheet, worksheet, header_row: int, headers: list[str]):
        self.spreadsheet = spreadsheet
        self.worksheet = worksheet
        self.header_row = header_row
        self.headers = headers

    @classmethod
    def connect(cls, settings: dict) -> "GoogleSheetCRM":
        path = project_path(settings["credentials_file"])
        email = _service_account_email(path)
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise CRMError("gspread and google-auth are not installed. Run: pip install -r requirements.txt") from exc
        if not path.exists():
            raise CRMError(_missing_credentials_message(path))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        try:
            credentials = Credentials.from_service_account_file(str(path), scopes=scopes)
            client = gspread.authorize(credentials)
            spreadsheet = client.open_by_key(settings["spreadsheet_id"])
        except Exception as exc:
            raise CRMError(_connect_error(exc, path, email, settings["spreadsheet_id"])) from exc
        worksheet = _open_worksheet(spreadsheet, settings.get("worksheet_name") or "")
        try:
            values = worksheet.get_all_values()
        except Exception as exc:
            raise CRMError(f"Could not read the worksheet {worksheet.title!r}: {exc}") from exc
        header_index = find_header_row(values)
        if header_index is None:
            if any(any(str(cell).strip() for cell in row) for row in values):
                raise CRMError(
                    f"Worksheet {worksheet.title!r} has data but no 'App Name' and 'App Store URL' headers. "
                    "The sheet was not changed."
                )
            try:
                headers = install_blank_headers(worksheet)
            except Exception as exc:
                raise CRMError(f"Could not write the header row: {exc}") from exc
            return cls(spreadsheet, worksheet, 1, headers)
        headers = [str(cell).strip() for cell in values[header_index]]
        while headers and not headers[-1]:
            headers.pop()
        missing = headers_to_add(headers)
        if missing:
            headers = _append_headers(worksheet, header_index + 1, headers, missing)
        return cls(spreadsheet, worksheet, header_index + 1, headers)

    def load_rows(self) -> tuple[DedupIndex, int, list[dict]]:
        try:
            values = self.worksheet.get_all_values()
        except Exception as exc:
            raise CRMError(f"Could not read the CRM: {exc}") from exc
        index = DedupIndex()
        records = []
        count = 0
        for offset, row in enumerate(values[self.header_row :], start=self.header_row + 1):
            record = _row_record(self.headers, row, offset)
            if not record:
                continue
            count += 1
            records.append(record)
            platform = (record.get("platform_key") or record.get("parsed_platform"))
            app_id = record.get("app_store_id") or record.get("parsed_id")
            index.add(platform, app_id, record.get("url"), record.get("app_name"), record.get("developer"))
        return index, count, records

    def append_leads(self, leads: list[Lead], template: dict | None, suppressed: set[str]) -> tuple[int, int]:
        index, _count, _records = self.load_rows()
        rows = []
        skipped = 0
        for lead in leads:
            if index.contains_lead(lead):
                skipped += 1
                LOGGER.info("Skipped duplicate %s", lead.dedup_id())
                continue
            index.add_lead(lead)
            rows.append(build_import_row(lead, self.headers, template, suppressed))
        if not rows:
            return 0, skipped
        try:
            self.worksheet.append_rows(rows, value_input_option="RAW")
        except Exception as exc:
            raise CRMError(
                f"Google Sheets rejected the append after {skipped} duplicate(s) were skipped. "
                f"No earlier batch was sent in this call. Error: {exc}"
            ) from exc
        return len(rows), skipped

    def mark_sent(self, row_number: int, expected_url: str, sent_on: date) -> None:
        headers = {name: position + 1 for position, name in enumerate(self.headers)}
        for required in ("Status", "App Store URL"):
            if required not in headers:
                raise CRMError(f"The CRM is missing the {required} column, so the send was not recorded.")
        try:
            current = self.worksheet.cell(row_number, headers["App Store URL"]).value or ""
        except Exception as exc:
            raise CRMError(f"Could not re-read row {row_number} before updating it: {exc}") from exc
        if canonical_url(current) != canonical_url(expected_url) and current.strip() != (expected_url or "").strip():
            raise CRMError(f"Row {row_number} no longer matches {expected_url}. It was not updated.")
        updates = [
            {
                "range": f"{column_letter(headers['Status'])}{row_number}",
                "values": [[STATUS_SENT]],
            }
        ]
        if "Sent Date" in headers:
            updates.append(
                {
                    "range": f"{column_letter(headers['Sent Date'])}{row_number}",
                    "values": [[sent_on.isoformat()]],
                }
            )
        if "Last Contacted" in headers:
            updates.append(
                {
                    "range": f"{column_letter(headers['Last Contacted'])}{row_number}",
                    "values": [[sent_on.isoformat()]],
                }
            )
        try:
            self.worksheet.batch_update(updates, value_input_option="RAW")
        except Exception as exc:
            raise CRMError(f"The message was sent, but row {row_number} could not be updated: {exc}") from exc


def _row_record(headers: list[str], row: list[str], row_number: int) -> dict | None:
    padded = list(row) + [""] * max(0, len(headers) - len(row))
    data = {header: padded[index].strip() for index, header in enumerate(headers) if header}
    if not data.get("App Name") and not data.get("App Store URL") and not data.get("App Store ID"):
        return None
    platform, parsed_id = identity_from_url(data.get("App Store URL"))
    platform_key = {"ios": "ios", "iOS": "ios", "android": "android", "Android": "android"}.get(data.get("Platform", ""))
    return {
        "row_number": row_number,
        "app_name": data.get("App Name", ""),
        "developer": data.get("Developer", ""),
        "email": data.get("Target Email") or data.get("Contact Email", ""),
        "contact_name": data.get("Contact Name", ""),
        "url": data.get("App Store URL", ""),
        "status": data.get("Status", ""),
        "subject": data.get("Email Subject", ""),
        "body": data.get("Generated Body", ""),
        "sent_date": data.get("Sent Date", ""),
        "app_store_id": data.get("App Store ID", ""),
        "platform_key": platform_key,
        "parsed_platform": platform,
        "parsed_id": parsed_id,
    }


def install_blank_headers(worksheet) -> list[str]:
    """Write and style the header row on a worksheet that has no CRM header."""
    width = len(BLANK_HEADERS)
    column_count = getattr(worksheet, "col_count", width) or width
    if column_count < width and hasattr(worksheet, "add_cols"):
        worksheet.add_cols(width - column_count)
    worksheet.update(values=[BLANK_HEADERS], range_name="A1", value_input_option="RAW")
    apply_crm_layout(worksheet, BLANK_HEADERS, header_row=1)
    LOGGER.info("Created CRM header row with %s columns.", width)
    return list(BLANK_HEADERS)


def _append_headers(worksheet, header_row: int, headers: list[str], missing: list[str]) -> list[str]:
    start = len(headers) + 1
    end = start + len(missing) - 1
    try:
        if worksheet.col_count < end:
            worksheet.add_cols(end - worksheet.col_count)
        range_name = f"{column_letter(start)}{header_row}:{column_letter(end)}{header_row}"
        worksheet.update(values=[missing], range_name=range_name, value_input_option="RAW")
    except Exception as exc:
        raise CRMError(f"Could not add columns {', '.join(missing)}: {exc}") from exc
    LOGGER.info("Added CRM columns: %s", ", ".join(missing))
    return headers + missing


def _open_worksheet(spreadsheet, name: str):
    try:
        if name:
            return spreadsheet.worksheet(name)
        return spreadsheet.get_worksheet(0)
    except Exception as exc:
        titles = []
        try:
            titles = [item.title for item in spreadsheet.worksheets()]
        except Exception:
            titles = []
        available = f" Available worksheets: {', '.join(titles)}." if titles else ""
        raise CRMError(f"Could not open worksheet {name or '(first sheet)'}: {exc}.{available}") from exc


def _service_account_email(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    email = data.get("client_email")
    return email if isinstance(email, str) else ""


def _missing_credentials_message(path: Path) -> str:
    return (
        "Google Sheets is not connected, so scraping did not start.\n\n"
        f"The service account file was not found at {path}.\n"
        "Create a service account, enable the Google Sheets API, download the JSON key, "
        f"and save it at {path}. Then share the spreadsheet with the client_email as an Editor.\n"
        "See README.md for the full setup."
    )


def _connect_error(exc: Exception, path: Path, email: str, spreadsheet_id: str) -> str:
    share = f"\nShare the spreadsheet as an Editor with {email}." if email else ""
    return (
        "Google Sheets is not connected, so scraping did not start.\n\n"
        f"Reason: {exc}\n"
        f"Credentials: {path}\n"
        f"Spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        f"{share}\n"
        "Confirm that the Google Sheets API is enabled and that the file is the service-account key, "
        "not an OAuth client file."
    )
