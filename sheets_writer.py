"""Google Sheets writer — upserts Frame.io video metadata into a spreadsheet.

Each Frame.io project maps to a tab matched by name (case-insensitive). The
``updates`` dict is keyed by Google Sheet column name (built in enrichment.py
from config.json); each key is matched to a header cell by normalized name.
Rows are upserted by Frame.io File ID (config.FILE_ID_COLUMN).
"""
import os
import json
import logging
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

SHEET_ID = os.environ.get("SHEET_ID", "")
_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Append-only event log tab (optional; not wired into the webhook flow).
EVENTS_TAB = "webhook events"

# Caches populated on first lookup. The service and tab list are built once;
# header maps are built lazily per tab name.
# Only the authenticated service is cached. Tab lists and header maps are NOT
# cached across calls: on warm serverless instances a persisted list goes stale
# when tabs/headers change in the sheet, which is confusing during setup. The
# webhook is low-volume, so re-fetching per call is cheap.
_service_cache = None


def _normalize(col: str) -> str:
    """Normalize a column name for fuzzy matching.

    Lowercases and removes underscores and *all* whitespace (including
    non-breaking spaces), so a tab/header typed with stray or unicode spaces
    still matches.
    """
    return "".join(col.split()).lower().replace("_", "")


def _col_letter(idx: int) -> str:
    """Convert a 0-based column index to an A1 column letter (handles AA+)."""
    letter = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letter = chr(65 + rem) + letter
    return letter


def _service():
    """Build (and cache) the Sheets API service."""
    global _service_cache
    if _service_cache is None:
        if not _CREDS_JSON:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        creds = service_account.Credentials.from_service_account_info(
            json.loads(_CREDS_JSON), scopes=_SCOPES
        )
        _service_cache = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service_cache


def _fetch_tabs() -> list:
    """Fetch the list of tab titles in the spreadsheet (live, not cached)."""
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")

    meta = _service().spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if not titles:
        raise RuntimeError(f"No tabs found in spreadsheet {SHEET_ID}")

    logger.info(f"Discovered sheet tabs: {titles}")
    return titles


def _find_tab(table_hint: str | None) -> str | None:
    """Pick the target tab.

    With a hint, match a tab by name case-insensitively (spaces and underscores
    ignored). Returns None if a hint is given but nothing matches. Without a
    hint, returns the first tab.
    """
    tabs = _fetch_tabs()
    if not table_hint:
        return tabs[0]

    target = _normalize(table_hint)
    for title in tabs:
        if _normalize(title) == target:
            return title
    return None


def _columns_for(tab: str) -> dict[str, int]:
    """Build {normalized header name → 0-based column index} for a tab (live)."""
    result = (
        _service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{tab}'!1:1")
        .execute()
    )
    header_row = (result.get("values") or [[]])[0]
    columns = {_normalize(h): i for i, h in enumerate(header_row) if h}
    logger.info(f"Tab {tab!r} headers: {header_row}")
    return columns


def discover_tab(table_hint: str | None = None) -> tuple[str, dict[str, int]]:
    """Resolve the target tab and its column map.

    With `table_hint` (e.g. a Frame.io project name), the tab is matched by name
    case-insensitively. Returns (tab_title, columns) where `columns` maps a
    normalized header name to its 0-based column index.
    Raises LookupError if a hint is given but no tab matches.
    """
    tab = _find_tab(table_hint)
    if tab is None:
        raise LookupError(f"No sheet tab matches {table_hint!r}")
    return tab, _columns_for(tab)


def _find_all_rows_by_file_ids(tab: str, file_id_col_idx: int, file_ids) -> list[int]:
    """Return all 1-based row numbers whose File ID column matches any given id,
    ordered top to bottom.

    A version stack lists every version's File ID, so this can return several
    rows (one per prior version that made it into the sheet). The header row
    (index 0) is skipped.
    """
    targets = {fid.strip() for fid in file_ids if fid and fid.strip()}
    if not targets:
        return []
    col = _col_letter(file_id_col_idx)
    result = (
        _service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{tab}'!{col}:{col}")
        .execute()
    )
    values = result.get("values", [])
    return [i + 1 for i, r in enumerate(values) if i and r and r[0].strip() in targets]


def _delete_rows(tab: str, row_indices: list[int]) -> None:
    """Delete the given 1-based rows from a tab.

    Deletes bottom-up so earlier deletions don't shift the indices of rows still
    to be removed.
    """
    if not row_indices:
        return
    sheet_id = _tab_sheet_id(tab)
    if sheet_id is None:
        logger.warning(f"Could not resolve sheetId for tab {tab!r}; cannot delete rows")
        return
    _service().spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": r - 1,
                            "endIndex": r,
                        }
                    }
                }
                for r in sorted(row_indices, reverse=True)
            ]
        },
    ).execute()


def upsert_record(
    updates: dict,
    table_hint: str | None = None,
    also_match_file_ids: list | None = None,
) -> str:
    """Write Frame.io metadata to a Google Sheet.

    `table_hint` (the Frame.io project name) selects which tab to write to,
    matched by name case-insensitively. If a hint is given but no tab matches,
    the write is skipped. With no hint, the first tab is used.

    Finds an existing row by File ID and updates it, or inserts a new one.
    Returns 'updated', 'inserted', or 'skipped'.

    `also_match_file_ids` lets a version-stack update locate the existing row by
    a prior version's File ID. When matched, the row's File ID cell is rewritten
    to the new id (it is part of `updates`), so the row carries the latest
    version. Default None keeps the plain by-File-ID behavior unchanged.

    `updates` is keyed by Google Sheet column name (see config.json).
    """
    file_id = updates.get(config.FILE_ID_COLUMN, "")
    if not file_id:
        raise ValueError(f"updates must include the file-id column {config.FILE_ID_COLUMN!r}")

    try:
        tab, columns = discover_tab(table_hint)
    except LookupError:
        try:
            available = _fetch_tabs()
        except Exception:
            available = "<unavailable>"
        logger.warning(
            f"No sheet tab matches project {table_hint!r} for file {file_id} "
            f"(available: {available}) — skipping"
        )
        return "skipped"

    # Match each update's column name to a real header, building {col idx: value}.
    # A field Frame.io reports as empty ("") is written through so the sheet
    # cell is cleared to match; only genuinely-absent fields (None) are skipped.
    cells: dict[int, str] = {}
    for col_name, value in updates.items():
        if value is None:
            continue
        idx = columns.get(_normalize(col_name))
        if idx is None:
            logger.warning(f"Tab {tab!r}: no column matches {col_name!r} — skipping that field")
            continue
        cells[idx] = value
    if not cells:
        logger.warning(f"No writable fields for file {file_id} — skipping")
        return "skipped"

    file_id_col_idx = columns.get(_normalize(config.FILE_ID_COLUMN))
    if file_id_col_idx is None:
        logger.warning(
            f"Tab {tab!r} has no {config.FILE_ID_COLUMN!r} column; cannot upsert "
            f"file {file_id} — skipping"
        )
        return "skipped"

    match_ids = [file_id, *(also_match_file_ids or [])]
    rows = _find_all_rows_by_file_ids(tab, file_id_col_idx, match_ids)

    if not rows:
        # Build a full row positioned by header index and append.
        width = max(cells) + 1
        new_row = [""] * width
        for idx, value in cells.items():
            new_row[idx] = value
        _service().spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"'{tab}'!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [new_row]},
        ).execute()
        logger.info(f"Inserted new row in {tab!r} for file {file_id}")
        return "inserted"

    # Keep the top-most matching row and update it to the latest version. Any
    # other matches are prior versions of the same stack, so collapse them —
    # only one row survives, carrying the newest version.
    row_index = rows[0]

    # Write every mapped cell, including ones cleared in Frame.io (empty string),
    # so the row mirrors the current Frame.io state.
    data = [
        {"range": f"'{tab}'!{_col_letter(idx)}{row_index}", "values": [[value]]}
        for idx, value in cells.items()
    ]
    _service().spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()

    # Deleting only rows below row_index (rows is ascending), so row_index stays valid.
    dupes = rows[1:]
    if dupes:
        _delete_rows(tab, dupes)
        logger.info(
            f"Collapsed {len(dupes)} prior-version row(s) in {tab!r} into row "
            f"{row_index} for file {file_id}"
        )

    logger.info(f"Updated row {row_index} in {tab!r} for file {file_id} ({len(data)} fields)")
    return "updated"


def _tab_sheet_id(tab: str) -> int | None:
    """Return the numeric sheetId (gid) for a tab title, or None if not found.

    The gid is required for structural requests like deleting a row (the values
    API works by title, but deleteDimension needs the gid).
    """
    meta = _service().spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == tab:
            return props.get("sheetId")
    return None


def append_event_row(event: dict):
    """Append every webhook to the events tab (optional; not wired into the flow)."""
    timestamp = datetime.now(timezone.utc).isoformat()
    row = [
        event.get("type", ""),
        timestamp,
        json.dumps(event)[:50000],  # cell limit safety
    ]
    _service().spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{EVENTS_TAB}'!A:C",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()
    logger.info(f"Appended event {event.get('type')} to {EVENTS_TAB}")
