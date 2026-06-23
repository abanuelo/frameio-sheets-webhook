"""Google Sheets writer — upserts Frame.io video metadata into a spreadsheet.

Mirrors ``airtable_writer``: each Frame.io project maps to a tab matched by name
(case-insensitive), the tab's header row maps internal keys to columns, and rows
are upserted by Frame.io File ID.
"""
import os
import json
import logging
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SHEET_ID = os.environ.get("SHEET_ID", "")
_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Append-only event log tab (optional; not wired into the webhook flow).
EVENTS_TAB = "webhook events"

# Internal key → normalized target for matching against sheet header names.
# Kept identical to airtable_writer._INTERNAL_KEYS so both writers consume the
# same `updates` dict built in enrichment.py.
_INTERNAL_KEYS: dict[str, str] = {
    "frameio_file_id": "fileid",
    "production_id": "name",
    "sme": "sme",
    "pm": "pm",
    "status": "status",
    "notes": "notes",
    "module": "module",
    "id": "id",
}

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


def _header_map_for(tab: str) -> dict[str, int]:
    """Build the internal-key → 0-based column index map for a tab (live)."""
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

    header_map: dict[str, int] = {}
    for internal_key, norm_target in _INTERNAL_KEYS.items():
        idx = columns.get(norm_target)
        if idx is not None:
            header_map[internal_key] = idx
        else:
            logger.warning(
                f"Tab {tab!r}: no header matches internal key {internal_key!r} "
                f"(looking for normalized {norm_target!r})"
            )

    logger.info(f"Header map for {tab!r}: {header_map}")
    return header_map


def discover_tab(table_hint: str | None = None) -> tuple[str, dict[str, int]]:
    """Resolve the target tab and its header map.

    With `table_hint` (e.g. a Frame.io project name), the tab is matched by name
    case-insensitively. Returns (tab_title, header_map).
    Raises LookupError if a hint is given but no tab matches.
    """
    tab = _find_tab(table_hint)
    if tab is None:
        raise LookupError(f"No sheet tab matches {table_hint!r}")
    return tab, _header_map_for(tab)


def _find_row_by_file_id(tab: str, file_id_col_idx: int, file_id: str) -> int | None:
    """Return the 1-based row number whose File ID column matches, or None."""
    col = _col_letter(file_id_col_idx)
    result = (
        _service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{tab}'!{col}:{col}")
        .execute()
    )
    values = result.get("values", [])
    target = file_id.strip()
    # Skip the header row (index 0).
    for i, r in enumerate(values):
        if i == 0:
            continue
        if r and r[0].strip() == target:
            return i + 1
    return None


def upsert_record(updates: dict, table_hint: str | None = None) -> str:
    """Write Frame.io metadata to a Google Sheet.

    `table_hint` (the Frame.io project name) selects which tab to write to,
    matched by name case-insensitively. If a hint is given but no tab matches,
    the write is skipped. With no hint, the first tab is used.

    Finds an existing row by File ID and updates it, or inserts a new one.
    Returns 'updated', 'inserted', or 'skipped'.
    """
    file_id = updates.get("frameio_file_id", "")
    if not file_id:
        raise ValueError("updates must include frameio_file_id")

    try:
        tab, header_map = discover_tab(table_hint)
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

    # Build {column_index: value} from our internal keys.
    cells = {
        idx: updates[key]
        for key, idx in header_map.items()
        if updates.get(key)
    }
    if not cells:
        logger.warning(f"No writable fields for file {file_id} — skipping")
        return "skipped"

    file_id_col_idx = header_map.get("frameio_file_id")
    if file_id_col_idx is None:
        logger.warning(
            f"Tab {tab!r} has no File ID column; cannot upsert file {file_id} — skipping"
        )
        return "skipped"

    row_index = _find_row_by_file_id(tab, file_id_col_idx, file_id)

    if row_index is None:
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

    # Update only the cells that have values — don't overwrite with blanks.
    data = [
        {"range": f"'{tab}'!{_col_letter(idx)}{row_index}", "values": [[value]]}
        for idx, value in cells.items()
    ]
    _service().spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    logger.info(f"Updated row {row_index} in {tab!r} for file {file_id} ({len(data)} fields)")
    return "updated"


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
