"""User configuration loader.

Reads ``config.json`` (Frame.io field → Google Sheet column mappings) with a
safe built-in default, so the app still runs if that file is missing or
malformed. Loaded once at import.

Non-developers should only ever need to edit ``config.json`` — never this file.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

# Fallback used when config.json is absent or the key is omitted. Mirrors the
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
# Sheet column that stores the Frame.io File ID (used to find/update the row,
# and to collapse version stacks). Fixed — not configurable.
FILE_ID_COLUMN: str = "File ID"
# Sheet column for the asset's filename. Fixed — not configurable.
FILENAME_COLUMN: str = "Name"
