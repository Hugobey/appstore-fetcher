"""Notion-style CRM layout: headers, status dropdown, and row colors.

Lead cells are never cleared or rewritten here. Repeated runs replace the
conditional-format rules instead of stacking new ones.
"""

from __future__ import annotations

import logging

from modules.errors import CRMError

LOGGER = logging.getLogger("prospecting.sheets")

CRM_COLUMNS = [
    "App Name",
    "App Store URL",
    "Contact Email",
    "Contact Name",
    "Status",
    "Follow-Up Date",
    "Last Contacted",
    "Lead Score",
    "Estimated MRR",
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
STATUS_OPTIONS = [
    "To Contact",
    "Sent",
    "Follow-Up 1",
    "Follow-Up 2",
    "Replied",
    "Call Booked",
    "Closed / Won",
    "Not Interested",
]
LAYOUT_ROWS = 1000
HEADER_BACKGROUND = "#1E293B"
HEADER_TEXT = "#FFFFFF"
# Wider columns first, then the compact numeric and status columns.
COLUMN_WIDTHS = {
    "App Name": 240,
    "App Store URL": 320,
    "Contact Email": 240,
    "Notes": 320,
    "Status": 140,
    "Lead Score": 110,
    "Estimated MRR": 130,
    "Ratings": 90,
}
DEFAULT_COLUMN_WIDTH = 150
_STATUS_COLORS = {
    "To Contact": ("#F8F9FA", None, False),
    "Sent": ("#E8F0FE", "#1967D2", False),
    "Follow-Up 1": ("#E8F0FE", "#1967D2", False),
    "Follow-Up 2": ("#E8F0FE", "#1967D2", False),
    "Replied": ("#FEF7E0", "#B06000", False),
    "Call Booked": ("#FEF7E0", "#B06000", False),
    "Closed / Won": ("#E6F4EA", "#137333", False),
    "Not Interested": ("#FCE8E6", "#80868B", True),
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
