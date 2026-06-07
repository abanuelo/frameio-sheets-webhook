"""Airtable writer — upserts Frame.io video metadata into an Airtable base."""
import os
import logging
import requests

logger = logging.getLogger(__name__)

AIRTABLE_API = "https://api.airtable.com/v0"

PAT = os.environ.get("AIRTABLE_PAT", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")

# Cached after first call to discover_table()
_table_name: str | None = None


def _headers() -> dict:
    return {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}


def discover_table() -> str:
    """Fetch the first table name from the base via the metadata API.

    Caches the result so subsequent calls don't hit the API again.
    """
    global _table_name
    if _table_name:
        return _table_name

    resp = requests.get(
        f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    tables = resp.json().get("tables", [])
    if not tables:
        raise RuntimeError(f"No tables found in Airtable base {BASE_ID}")

    _table_name = tables[0]["name"]
    logger.info(f"Discovered Airtable table: {_table_name!r}")
    return _table_name


def _find_record_by_file_id(table_name: str, file_id: str) -> dict | None:
    """Find an existing record where the 'File ID' field matches."""
    resp = requests.get(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        params={"filterByFormula": f"{{File ID}}='{file_id}'", "maxRecords": 1},
        timeout=15,
    )
    resp.raise_for_status()
    records = resp.json().get("records", [])
    return records[0] if records else None


def _create_record(table_name: str, fields: dict) -> dict:
    resp = requests.post(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        json={"records": [{"fields": fields}]},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _update_record(table_name: str, record_id: str, fields: dict) -> dict:
    resp = requests.patch(
        f"{AIRTABLE_API}/{BASE_ID}/{requests.utils.quote(table_name)}",
        headers=_headers(),
        json={"records": [{"id": record_id, "fields": fields}]},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# Maps our internal update key → Airtable column name
_FIELD_MAP: dict[str, str] = {
    "frameio_file_id": "File ID",
    "production_id": "Name",
    "sme": "SME",
    "pm": "PM",
    "status": "Status",
    "notes": "Notes",
}


def upsert_record(updates: dict) -> str:
    """Write Frame.io metadata to Airtable.

    Finds an existing record by File ID and updates it, or creates a new one.
    Returns 'updated', 'created', or 'skipped'.
    """
    file_id = updates.get("frameio_file_id", "")
    if not file_id:
        raise ValueError("updates must include frameio_file_id")

    # Build Airtable fields dict from our internal keys
    fields: dict[str, str] = {}
    for key, col_name in _FIELD_MAP.items():
        value = updates.get(key)
        if value:
            fields[col_name] = value

    if not fields:
        logger.warning(f"No writable fields for file {file_id} — skipping")
        return "skipped"

    table_name = discover_table()

    existing = _find_record_by_file_id(table_name, file_id)

    if existing:
        record_id = existing["id"]
        _update_record(table_name, record_id, fields)
        logger.info(f"Updated Airtable record {record_id} for file {file_id}")
        return "updated"
    else:
        _create_record(table_name, fields)
        logger.info(f"Created new Airtable record for file {file_id}")
        return "created"
