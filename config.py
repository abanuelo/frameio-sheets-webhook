"""User configuration loader.

Reads ``config.json`` (Frame.io field → Google Sheet column mappings, plus the
status rules) with safe built-in defaults, so the app still runs if that file is
missing or malformed. Loaded once at import.

Non-developers should only ever need to edit ``config.json`` — never this file.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

# Fallbacks used when config.json is absent or a key is omitted. These mirror the
# original hard-coded behavior.
_DEFAULTS = {
    "field_mappings": {
        "Status": "Status",
        "PM": "PM",
        "SME": "SME",
        "Notes": "Notes",
        "MODULE": "Module",
        "ID": "ID",
    },
    "file_id_column": "File ID",
    "filename_column": "Name",
    "status_column": "Status",
    "removal_statuses": ["Full Length Lecture"],
    "deletable_prior_statuses": ["R1 Edits", "R2 Edits"],
}

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _load() -> dict:
    cfg = {**_DEFAULTS}
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        logger.info("config.json not found — using built-in defaults")
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"config.json could not be read ({e}) — using built-in defaults")
        return cfg

    for key in _DEFAULTS:
        if key in user:
            cfg[key] = user[key]
    logger.info(f"Loaded config.json (keys: {sorted(k for k in user if not k.startswith('_'))})")
    return cfg


_cfg = _load()

# Frame.io metadata field name → Google Sheet column header.
FIELD_MAPPINGS: dict = _cfg["field_mappings"]
# Sheet column that stores the Frame.io File ID (used to find/update the row).
FILE_ID_COLUMN: str = _cfg["file_id_column"]
# Sheet column for the asset's filename ("" to skip writing it).
FILENAME_COLUMN: str = _cfg["filename_column"]
# Sheet column that holds the status (drives the deletion rules below).
STATUS_COLUMN: str = _cfg["status_column"]
# When the status becomes one of these, the row may be deleted (see below).
REMOVAL_STATUSES: tuple = tuple(_cfg["removal_statuses"])
# ...but only if the row's *current* status is one of these first.
DELETABLE_PRIOR_STATUSES: tuple = tuple(_cfg["deletable_prior_statuses"])
