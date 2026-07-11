"""Airtable writer — upserts Frame.io video metadata into an Airtable base.

`updates` is keyed by column name (built in enrichment.py from config.json);
each key is matched to a table field by normalized name. Retained but disabled
by default (AIRTABLE_ENABLED); Google Sheets is the active backend.
"""
import os
import logging
import requests

import config

logger = logging.getLogger(__name__)

AIRTABLE_API = "https://api.airtable.com/v0"

PAT = os.environ.get("AIRTABLE_PAT", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")

# Table lists and field maps are fetched live on every call (not cached): on
# warm serverless instances a persisted list goes stale when tables/columns
# change in the base. The webhook is low-volume, so re-fetching is cheap.


def _normalize(col: str) -> str:
    """Normalize a column name for fuzzy matching.

    Lowercases and removes underscores and *all* whitespace (including
    non-breaking spaces), so a table/column typed with stray or unicode spaces
    still matches.
    """
    return "".join(col.split()).lower().replace("_", "")


def _raise_on_error(resp: requests.Response) -> None:
    """Raise with the full Airtable error body instead of just the status line."""
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"Airtable API error {resp.status_code}: {body}")


def _headers() -> dict:
    return {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}


def _fetch_tables() -> list:
    """Fetch the list of tables in the base via the meta API (live, not cached)."""
    resp = requests.get(
        f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables",
        headers=_headers(),
        timeout=15,
    )
    _raise_on_error(resp)
    tables = resp.json().get("tables", [])
    if not tables:
        raise RuntimeError(f"No tables found in Airtable base {BASE_ID}")

    logger.info(f"Discovered Airtable tables: {[t['name'] for t in tables]}")
    return tables


def _columns_for(table: dict) -> dict[str, str]:
    """Build {normalized column name → actual Airtable column name} for a table."""
    name = table["name"]
    columns = {_normalize(f["name"]): f["name"] for f in table.get("fields", [])}
    logger.info(f"Table {name!r} columns: {list(columns.values())}")
    return columns


def _find_table(table_hint: str | None) -> dict | None:
    """Pick the target table.

    With a hint, match an Airtable table by name case-insensitively (spaces and
    underscores ignored). Returns None if a hint is given but nothing matches.
    Without a hint, returns the first table in the base.
    """
    tables = _fetch_tables()
    if not table_hint:
        return tables[0]

    target = _normalize(table_hint)
    for table in tables:
        if _normalize(table["name"]) == target:
            return table
    return None


def discover_table(table_hint: str | None = None) -> tuple[str, dict[str, str]]:
    """Resolve the target table and its column map.

    With `table_hint` (e.g. a Frame.io project name), the table is matched by
    name case-insensitively. Returns (table_name, columns) where `columns` maps
    a normalized column name to the actual Airtable column name.
    Raises LookupError if a hint is given but no table matches.
    """
    table = _find_table(table_hint)
    if table is None:
        raise LookupError(f"No Airtable table matches {table_hint!r}")
    return table["name"], _columns_for(table)


def _find_record_by_file_id(table_name: str, file_id_col: str, file_id: str) -> dict | None:
    """Find an existing record where the file ID column matches."""
    resp = requests.get(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        params={"filterByFormula": f"{{{file_id_col}}}='{file_id}'", "maxRecords": 1},
        timeout=15,
    )
    _raise_on_error(resp)
    records = resp.json().get("records", [])
    return records[0] if records else None


def _create_record(table_name: str, fields: dict) -> dict:
    resp = requests.post(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        json={"records": [{"fields": fields}]},
        timeout=15,
    )
    _raise_on_error(resp)
    return resp.json()


def _update_record(table_name: str, record_id: str, fields: dict) -> dict:
    resp = requests.patch(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        json={"records": [{"id": record_id, "fields": fields}]},
        timeout=15,
    )
    _raise_on_error(resp)
    return resp.json()


def upsert_record(updates: dict, table_hint: str | None = None) -> str:
    """Write Frame.io metadata to Airtable.

    `table_hint` (the Frame.io project name) selects which table to write to,
    matched by name case-insensitively. If a hint is given but no table matches,
    the write is skipped. With no hint, the first table in the base is used.

    Finds an existing record by File ID and updates it, or creates a new one.
    Returns 'updated', 'created', or 'skipped'.

    `updates` is keyed by column name (see config.json).
    """
    file_id = updates.get(config.FILE_ID_COLUMN, "")
    if not file_id:
        raise ValueError(f"updates must include the file-id column {config.FILE_ID_COLUMN!r}")

    try:
        table_name, columns = discover_table(table_hint)
    except LookupError:
        try:
            available = [t["name"] for t in _fetch_tables()]
        except Exception:
            available = "<unavailable>"
        logger.warning(
            f"No Airtable table matches project {table_hint!r} for file {file_id} "
            f"(available: {available}) — skipping"
        )
        return "skipped"

    # Match each update's column name to an actual table field.
    fields: dict[str, str] = {}
    for col_name, value in updates.items():
        if not value:
            continue
        actual = columns.get(_normalize(col_name))
        if actual is None:
            logger.warning(f"Table {table_name!r}: no column matches {col_name!r} — skipping that field")
            continue
        fields[actual] = value

    if not fields:
        logger.warning(f"No writable fields for file {file_id} — skipping")
        return "skipped"

    file_id_col = columns.get(_normalize(config.FILE_ID_COLUMN), config.FILE_ID_COLUMN)
    existing = _find_record_by_file_id(table_name, file_id_col, file_id)

    if existing:
        record_id = existing["id"]
        _update_record(table_name, record_id, fields)
        logger.info(f"Updated Airtable record {record_id} for file {file_id}")
        return "updated"
    else:
        _create_record(table_name, fields)
        logger.info(f"Created new Airtable record for file {file_id}")
        return "created"
