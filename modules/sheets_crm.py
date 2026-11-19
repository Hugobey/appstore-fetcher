"""Notion-style CRM layout: headers, status dropdown, and row colors.

Lead cells are never cleared or rewritten here. Repeated runs replace the
conditional-format rules instead of stacking new ones.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from modules.errors import CRMError

LOGGER = logging.getLogger("prospecting.sheets")

CRM_COLUMNS = [
    "App Name",
    "Developer / Seller",
    "Category",
    "Average Rating",
    "Rating Count",
    "Bayesian Score",
    "Estimated MRR",
    "App Store URL",
    "Buy Signal",
    "Contact",
    "Contact Email",
    "Review count",
    "Status",
    "Platform",
    "Website",
    "Last contacted",
]
STATUS_OPTIONS = [
    "To Contact",
    "Sent",
    "Follow-Up",
    "Replied",
    "Booked",
    "Not Interested",
]
LAYOUT_ROWS = 1000
HEADER_BACKGROUND = "#1E293B"
HEADER_TEXT = "#FFFFFF"
# Wider columns first, then the compact numeric and status columns.
COLUMN_WIDTHS = {
    "App Name": 240,
    "Developer / Seller": 220,
    "Category": 160,
    "Average Rating": 120,
    "Rating Count": 110,
    "Bayesian Score": 140,
    "Estimated MRR": 180,
    "App Store URL": 320,
    "Buy Signal": 220,
    "Contact": 220,
    "Contact Email": 240,
    "Website": 240,
    "Status": 140,
    "Review count": 110,
    "Last contacted": 140,
}
DEFAULT_COLUMN_WIDTH = 150
_STATUS_COLORS = {
    "To Contact": ("#F8F9FA", None, False),
    "Sent": ("#E8F0FE", "#1967D2", False),
    "Follow-Up": ("#E8F0FE", "#1967D2", False),
    "Replied": ("#FEF7E0", "#B06000", False),
    "Booked": ("#E6F4EA", "#137333", False),
    "Not Interested": ("#FCE8E6", "#80868B", True),
}
_STATUS_REMAP = {
    "follow-up 1": "Follow-Up",
    "follow-up 2": "Follow-Up",
    "call booked": "Booked",
}


def rgb(hex_color: str) -> dict[str, float]:
    value = hex_color.lstrip("#")
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }


def column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def layout_requests(
    sheet_id: int,
    headers: list[str],
    header_row: int = 1,
    existing_rule_count: int = 0,
) -> list[dict]:
    """BatchUpdate requests for the current header row. Does not include cell values."""
    headers = [str(cell).strip() for cell in headers if str(cell).strip()]
    width = len(headers)
    if width == 0:
        return []
    first_data_row = max(1, int(header_row)) + 1
    start_row = first_data_row - 1
    requests: list[dict] = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
        for index in range(max(0, int(existing_rule_count)) - 1, -1, -1)
    ]
    status_index = _header_index(headers, "Status")
    if status_index is not None:
        status_letter = column_letter(status_index + 1)
        requests.append(
            {
                "setDataValidation": {
                    "range": _grid(sheet_id, start_row, LAYOUT_ROWS, status_index, status_index + 1),
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": status} for status in STATUS_OPTIONS],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            }
        )
        for status in STATUS_OPTIONS:
            background, text, strike = _STATUS_COLORS[status]
            formula = f'=${status_letter}{first_data_row}="{status}"'
            cell_format: dict = {"backgroundColor": rgb(background)}
            if text or strike:
                text_format: dict = {}
                if text:
                    text_format["foregroundColor"] = rgb(text)
                if strike:
                    text_format["strikethrough"] = True
                cell_format["textFormat"] = text_format
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [_grid(sheet_id, start_row, LAYOUT_ROWS, 0, width)],
                            "booleanRule": {
                                "condition": {
                                    "type": "CUSTOM_FORMULA",
                                    "values": [{"userEnteredValue": formula}],
                                },
                                "format": cell_format,
                            },
                        },
                        "index": 0,
                    }
                }
            )
    requests.append(
        {
            "repeatCell": {
                "range": _grid(sheet_id, header_row - 1, header_row, 0, width),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": rgb(HEADER_BACKGROUND),
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": rgb(HEADER_TEXT),
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
            }
        }
    )
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": int(header_row)},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    requests.append(
        {
            "setBasicFilter": {
                "filter": {
                    "range": _grid(sheet_id, header_row - 1, LAYOUT_ROWS, 0, width),
                }
            }
        }
    )
    for index, header in enumerate(headers):
        pixels = COLUMN_WIDTHS.get(header, DEFAULT_COLUMN_WIDTH)
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": pixels},
                    "fields": "pixelSize",
                }
            }
        )
    return requests


def apply_crm_layout(worksheet, headers: list[str], header_row: int = 1) -> list[dict]:
    """Style the worksheet. Existing lead values are left untouched."""
    clean = [str(cell).strip() for cell in headers if str(cell).strip()]
    if not clean:
        return []
    _ensure_grid(worksheet, len(clean))
    spreadsheet = getattr(worksheet, "spreadsheet", None)
    sheet_id = _sheet_id(worksheet)
    if spreadsheet is None or not hasattr(spreadsheet, "batch_update"):
        _paint_header(worksheet, len(clean))
        return layout_requests(sheet_id, clean, header_row, 0)
    try:
        rule_count = conditional_format_count(spreadsheet, sheet_id)
        requests = layout_requests(sheet_id, clean, header_row, rule_count)
        spreadsheet.batch_update({"requests": requests})
    except CRMError:
        raise
    except Exception as exc:
        raise CRMError(f"Could not format the CRM: {exc}") from exc
    LOGGER.info("Applied CRM layout on sheet %s without rewriting lead rows.", sheet_id)
    return requests


def conditional_format_count(spreadsheet, sheet_id: int) -> int:
    meta = spreadsheet.fetch_sheet_metadata(
        params={
            "includeGridData": "false",
            "fields": "sheets(properties.sheetId,conditionalFormats)",
        }
    )
    for sheet in meta.get("sheets") or []:
        properties = sheet.get("properties") or {}
        if properties.get("sheetId") == sheet_id:
            return len(sheet.get("conditionalFormats") or [])
    return 0


def _grid(sheet_id: int, start_row: int, end_row: int, start_column: int, end_column: int) -> dict:
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }


def _header_index(headers: list[str], name: str) -> int | None:
    wanted = name.strip().lower()
    for index, header in enumerate(headers):
        if header.strip().lower() == wanted:
            return index
    return None


def _sheet_id(worksheet) -> int:
    try:
        return int(worksheet.id)
    except Exception:
        return 0


def _ensure_grid(worksheet, width: int) -> None:
    row_count = getattr(worksheet, "row_count", None)
    if isinstance(row_count, int) and row_count < LAYOUT_ROWS and hasattr(worksheet, "add_rows"):
        worksheet.add_rows(LAYOUT_ROWS - row_count)
    col_count = getattr(worksheet, "col_count", None)
    if isinstance(col_count, int) and col_count < width and hasattr(worksheet, "add_cols"):
        worksheet.add_cols(width - col_count)


def _paint_header(worksheet, width: int) -> None:
    """Used when the worksheet client cannot send a spreadsheet batchUpdate."""
    end = column_letter(width)
    style = {
        "backgroundColor": rgb(HEADER_BACKGROUND),
        "verticalAlignment": "MIDDLE",
        "textFormat": {"bold": True, "foregroundColor": rgb(HEADER_TEXT)},
    }
    try:
        worksheet.format(f"A1:{end}1", style)
    except Exception as exc:
        LOGGER.warning("Could not style the CRM header row: %s", exc)
    try:
        worksheet.freeze(rows=1)
    except Exception as exc:
        LOGGER.warning("Could not freeze the CRM header row: %s", exc)


# CSV exports use the same columns, in the same order, as the Google Sheet.
EXPORT_COLUMNS = list(CRM_COLUMNS)
SHEETS_IMPORT_TIP = "File > Import > Upload"
_EXPORT_ALIASES = {
    "App Name": ("app_name", "App Name", "Appname"),
    "Developer / Seller": ("seller_name", "sellerName", "developer_name", "Developer / Seller", "Developer"),
    "Category": ("Category", "category"),
    "Average Rating": ("average_rating", "Average Rating", "averageUserRating"),
    "Rating Count": ("rating_count", "Rating Count", "Rating count", "Ratings", "ratings"),
    "Bayesian Score": ("bayesian_score", "Bayesian Score"),
    "Estimated MRR": ("estimated_mrr", "Estimated MRR", "mrr"),
    "App Store URL": ("app_store_url", "App Store URL", "Appstore URL"),
    "Buy Signal": ("buy_signal", "Buy Signal"),
    "Contact": ("Contact", "contact_name", "Contact name", "Contact Name"),
    "Contact Email": ("primary_email", "Contact Email", "contact email", "email", "Target Email"),
    "Review count": ("review_count", "Review count", "Review Count"),
    "Status": ("Status", "status"),
    "Platform": ("platform", "Platform"),
    "Website": ("developer_website", "Developer Website", "website URL", "website_url", "Website"),
    "Last contacted": ("Last contacted", "Last Contacted", "Sent Date", "sent_date"),
}


def lead_to_export_row(item) -> dict:
    """Map a Lead or a CRM dict onto the CRM columns. The first filled alias wins."""
    from modules.discovery import buy_signal
    from modules.enricher import bucket_mrr_text, offline_revenue_display
    from modules.scoring import bayesian_score

    source = _export_source(item)
    row = {column: _first_alias(source, _EXPORT_ALIASES[column]) for column in EXPORT_COLUMNS}
    row["Platform"] = _platform_label(row["Platform"])
    if not row["Status"]:
        row["Status"] = "To Contact"
    row["Average Rating"] = _format_average(row["Average Rating"])
    if _can_score_signal(source):
        row["Buy Signal"] = buy_signal(source)
    if _recompute_revenue(source) or not row["Estimated MRR"]:
        row["Estimated MRR"] = offline_revenue_display(source)
    row["Estimated MRR"] = bucket_mrr_text(row["Estimated MRR"])
    if not row["Bayesian Score"]:
        score = bayesian_score(row["Average Rating"], row["Rating Count"])
        row["Bayesian Score"] = "" if score is None else f"{score:.2f}"
    else:
        row["Bayesian Score"] = _format_score(row["Bayesian Score"])
    return row


def _can_score_signal(source: dict) -> bool:
    """A row that already carries a Buy Signal and no dates should keep that signal."""
    keys = (
        "released_days_ago",
        "updated_days_ago",
        "version_days_ago",
        "release_date",
        "last_update",
        "current_version_release_date",
    )
    return any(source.get(key) not in (None, "") for key in keys)


def _recompute_revenue(source: dict) -> bool:
    return "released_days_ago" in source or "updated_days_ago" in source


def _platform_label(value: str) -> str:
    folded = (value or "").strip().lower()
    if folded == "ios":
        return "iOS"
    if folded == "android":
        return "Android"
    return (value or "").strip()


def schema_is_current(headers: list[str]) -> bool:
    return _clean_headers(headers) == list(CRM_COLUMNS)


def migrate_row(headers: list[str], row: list) -> list[str]:
    """One physical row in the current column order.

    Lead Score is never copied into Buy Signal or Bayesian Score.
    """
    from modules.enricher import bucket_mrr_text

    lookup: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _norm_header(header)
        if key and key not in lookup:
            lookup[key] = index

    def cell(*names: str) -> str:
        for name in names:
            index = lookup.get(name)
            if index is None or index >= len(row):
                continue
            text = str(row[index]).strip()
            if text:
                return text
        return ""

    status = cell("status")
    status = _STATUS_REMAP.get(status.lower(), status)
    values = {
        "App Name": cell("app name", "appname"),
        "Developer / Seller": cell("developer / seller", "developer", "seller name", "seller"),
        "Category": cell("category"),
        "Average Rating": _format_average(cell("average rating")),
        "Rating Count": cell("rating count", "ratings"),
        "Bayesian Score": _format_score(cell("bayesian score")),
        "Estimated MRR": bucket_mrr_text(cell("estimated mrr")),
        "App Store URL": cell("app store url", "appstore url"),
        "Buy Signal": cell("buy signal"),
        "Contact": cell("contact") or cell("contact name"),
        "Contact Email": cell("contact email") or cell("target email"),
        "Review count": cell("review count"),
        "Status": status,
        "Platform": _platform_label(cell("platform")),
        "Website": cell("website", "developer website", "website url"),
        "Last contacted": cell("last contacted") or cell("sent date"),
    }
    return [values[column] for column in CRM_COLUMNS]


def migrate_sheet_values(values: list[list], header_index: int) -> list[list[str]]:
    """Header plus every physical data row, so sheet row numbers stay put."""
    headers = values[header_index] if header_index < len(values) else []
    grid = [list(CRM_COLUMNS)]
    for row in values[header_index + 1 :]:
        grid.append(migrate_row(headers, row))
    return grid


def _clean_headers(headers: list[str]) -> list[str]:
    clean = [str(cell).strip() for cell in headers]
    while clean and not clean[-1]:
        clean.pop()
    return clean


def _norm_header(header) -> str:
    return " ".join(str(header).strip().lower().split())


def write_export_csv(leads, path: Path) -> Path:
    from modules.scoring import quality_filter_reason

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for lead in leads:
        if quality_filter_reason(lead):
            continue
        rows.append(lead_to_export_row(lead))
    rows.sort(key=_export_sort_key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _export_sort_key(row: dict) -> tuple:
    raw = row.get("Bayesian Score")
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return (1, 0.0, row.get("App Name") or "")
    return (0, -score, row.get("App Name") or "")


def _format_average(value) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_score(value) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value).strip()


def deals_export_path(directory: Path | None = None, now: datetime | None = None) -> Path:
    """exports/deals_YYYY-MM-DD_HHMM.csv. A same-minute collision gets a numeric suffix."""
    from modules.config import project_path

    folder = directory or project_path("exports")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M")
    path = folder / f"deals_{stamp}.csv"
    suffix = 2
    while path.exists():
        path = folder / f"deals_{stamp}_{suffix}.csv"
        suffix += 1
    return path


def _export_source(item) -> dict:
    if isinstance(item, dict):
        return item
    data = {}
    for name in (
        "app_name",
        "developer_name",
        "seller_name",
        "average_rating",
        "bayesian_score",
        "estimated_mrr",
        "rating_count",
        "review_count",
        "score_normalized",
        "lead_score",
        "contact_name",
        "category",
        "app_store_url",
        "developer_website",
        "primary_email",
        "platform",
        "price",
        "has_iap",
        "release_date",
        "last_update",
        "current_version_release_date",
        "recent_ratings_30d",
        "developer_type",
        "buy_signal",
    ):
        if hasattr(item, name):
            data[name] = getattr(item, name)
    return data


def _first_alias(source: dict, aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        if alias in source:
            value = _present(source[alias])
            if value is not None:
                return value
    folded = {str(key).strip().lower(): key for key in source}
    for alias in aliases:
        key = folded.get(alias.strip().lower())
        if key is None:
            continue
        value = _present(source[key])
        if value is not None:
            return value
    return ""


def _present(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        if "address" not in value:
            return None
        value = value.get("address")
    elif hasattr(value, "address") and not isinstance(value, (str, bytes)):
        value = getattr(value, "address")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if text == "" or text.lower() == "none":
        return None
    return text
