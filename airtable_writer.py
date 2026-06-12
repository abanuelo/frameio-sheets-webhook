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
_field_map: dict[str, str] | None = None

# Internal key → normalized target for matching against Airtable column names
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


def _normalize(col: str) -> str:
    """Normalize a column name for fuzzy matching."""
    return col.lower().replace(" ", "").replace("_", "")


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


def discover_table() -> tuple[str, dict[str, str]]:
    """Fetch the first table name and build a dynamic field map.

    Caches both so subsequent calls don't hit the API again.
    Returns (table_name, field_map).
    """
    global _table_name, _field_map
    if _table_name and _field_map is not None:
        return _table_name, _field_map

    resp = requests.get(
        f"https://api.airtable.com/v0/meta/bases/{BASE_ID}/tables",
        headers=_headers(),
        timeout=15,
    )
    _raise_on_error(resp)
    tables = resp.json().get("tables", [])
    if not tables:
        raise RuntimeError(f"No tables found in Airtable base {BASE_ID}")

    table = tables[0]
    _table_name = table["name"]
    logger.info(f"Discovered Airtable table: {_table_name!r}")

    # Build normalized lookup: normalized_name → actual Airtable column name
    columns = {_normalize(f["name"]): f["name"] for f in table.get("fields", [])}
    logger.info(f"Airtable columns: {list(columns.values())}")

    # Match our internal keys to actual columns
    _field_map = {}
    for internal_key, norm_target in _INTERNAL_KEYS.items():
        actual = columns.get(norm_target)
        if actual:
            _field_map[internal_key] = actual
        else:
            logger.warning(
                f"No Airtable column matches internal key {internal_key!r} "
                f"(looking for normalized {norm_target!r})"
            )

    logger.info(f"Dynamic field map: {_field_map}")
    return _table_name, _field_map


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


def upsert_record(updates: dict) -> str:
    """Write Frame.io metadata to Airtable.

    Finds an existing record by File ID and updates it, or creates a new one.
    Returns 'updated', 'created', or 'skipped'.
    """
    file_id = updates.get("frameio_file_id", "")
    if not file_id:
        raise ValueError("updates must include frameio_file_id")

    table_name, field_map = discover_table()

    # Build Airtable fields dict from our internal keys
    fields: dict[str, str] = {}
    for key, col_name in field_map.items():
        value = updates.get(key)
        if value:
            fields[col_name] = value

    if not fields:
        logger.warning(f"No writable fields for file {file_id} — skipping")
        return "skipped"

    file_id_col = field_map.get("frameio_file_id", "File ID")
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
