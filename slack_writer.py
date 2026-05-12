"""Slack Lists writer — upserts Frame.io video metadata into a Slack List."""
import os
import logging
import requests

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
LIST_ID = os.environ.get("SLACK_LIST_ID", "")

# Column IDs (populated by running discover_schema.py)
COL_NAME     = os.environ.get("SLACK_COL_NAME", "")
COL_FILE_ID  = os.environ.get("SLACK_COL_FILE_ID", "")
COL_SME      = os.environ.get("SLACK_COL_SME", "")
COL_PM       = os.environ.get("SLACK_COL_PM", "")
COL_STATUS   = os.environ.get("SLACK_COL_STATUS", "")
COL_NOTES    = os.environ.get("SLACK_COL_NOTES", "")

# Select option IDs for SME column (each column has distinct option IDs in Slack)
_SME_OPTIONS: dict[str, str] = {
    "Needs Review": os.environ.get("SLACK_SME_OPT_NEEDS_REVIEW", ""),
    "In Progress":  os.environ.get("SLACK_SME_OPT_IN_PROGRESS", ""),
    "Approved":     os.environ.get("SLACK_SME_OPT_APPROVED", ""),
    "N/A":          os.environ.get("SLACK_SME_OPT_NA", ""),
}

# Select option IDs for PM column
_PM_OPTIONS: dict[str, str] = {
    "Needs Review": os.environ.get("SLACK_PM_OPT_NEEDS_REVIEW", ""),
    "In Progress":  os.environ.get("SLACK_PM_OPT_IN_PROGRESS", ""),
    "Approved":     os.environ.get("SLACK_PM_OPT_APPROVED", ""),
    "N/A":          os.environ.get("SLACK_PM_OPT_NA", ""),
}

# Select option IDs for the Status (Overall Video Status) column
_STATUS_OPTIONS: dict[str, str] = {
    "Rough Cut Ready":     os.environ.get("SLACK_STATUS_OPT_ROUGH_CUT_READY", ""),
    "R1 Comments":         os.environ.get("SLACK_STATUS_OPT_R1_COMMENTS", ""),
    "R2 Comments":         os.environ.get("SLACK_STATUS_OPT_R2_COMMENTS", ""),
    "R2 Edits":            os.environ.get("SLACK_STATUS_OPT_R2_EDITS", ""),
    "Approvals":           os.environ.get("SLACK_STATUS_OPT_APPROVALS", ""),
    "Full Length Lecture": os.environ.get("SLACK_STATUS_OPT_FULL_LENGTH_LECTURE", ""),
}

# Maps our internal update key to (column_id_var, field_type)
_FIELD_CONFIG: dict[str, tuple[str, str]] = {
    "production_id":   (COL_NAME,    "text"),
    "frameio_file_id": (COL_FILE_ID, "text"),
    "sme":             (COL_SME,     "select"),
    "pm":              (COL_PM,      "select"),
    "status":          (COL_STATUS,  "select"),
    "notes":           (COL_NOTES,   "text"),
}


def _slack_post(method: str, payload: dict) -> dict:
    resp = requests.post(
        f"{SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error ({method}): {data.get('error')} — {data}")
    return data


def _list_all_items(list_id: str) -> list[dict]:
    """Paginate through all items in the list and return them."""
    items: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {"list_id": list_id, "limit": 100}
        if cursor:
            payload["cursor"] = cursor
        # items.list uses GET with query params but we post via JSON; use GET instead
        resp = requests.get(
            f"{SLACK_API}/slackLists.items.list",
            headers={"Authorization": f"Bearer {TOKEN}"},
            params=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"slackLists.items.list error: {data.get('error')}")
        items.extend(data.get("items", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return items


def _get_text_value(field: dict) -> str:
    """Extract plain text from a rich_text field or a raw value field."""
    text = ""
    for block in field.get("rich_text", []):
        text += block.get("text", "")
    if not text:
        text = str(field.get("value", "") or "")
    return text


def _find_item_by_file_id(file_id: str) -> dict | None:
    """Search all list items for one whose File ID column matches file_id."""
    if not COL_FILE_ID:
        logger.error("SLACK_COL_FILE_ID env var not set — cannot search list")
        return None

    items = _list_all_items(LIST_ID)
    for item in items:
        for field in item.get("fields", []):
            if field.get("column_id") == COL_FILE_ID:
                if _get_text_value(field) == file_id:
                    return item
    return None


def _build_text_field(column_id: str, value: str) -> dict:
    return {
        "column_id": column_id,
        "value": str(value),
    }


def _build_select_field(column_id: str, option_id: str) -> dict:
    return {
        "column_id": column_id,
        "select": [option_id],
    }


def _resolve_option_id(field_key: str, value: str) -> str | None:
    """Map a Frame.io display value to the corresponding Slack option ID."""
    if not value:
        return None
    if field_key == "status":
        opt_id = _STATUS_OPTIONS.get(value, "")
    elif field_key == "sme":
        opt_id = _SME_OPTIONS.get(value, "")
    else:  # pm
        opt_id = _PM_OPTIONS.get(value, "")
    if not opt_id:
        logger.warning(f"No option ID found for field={field_key!r} value={value!r} — skipping")
        return None
    return opt_id


def _build_fields(updates: dict) -> list[dict]:
    """Convert the updates dict into a list of Slack field objects."""
    fields = []
    for key, (col_id, field_type) in _FIELD_CONFIG.items():
        value = updates.get(key)
        if not value or not col_id:
            continue
        if field_type == "text":
            fields.append(_build_text_field(col_id, value))
        elif field_type == "select":
            opt_id = _resolve_option_id(key, value)
            if opt_id:
                fields.append(_build_select_field(col_id, opt_id))
    return fields


def upsert_list_item(updates: dict) -> str:
    """
    Write Frame.io metadata to the Slack list.
    Finds an existing row by file_id and updates it, or creates a new row.
    Returns 'updated' or 'created'.
    """
    file_id = updates.get("frameio_file_id", "")
    if not file_id:
        raise ValueError("updates must include frameio_file_id")

    fields = _build_fields(updates)
    if not fields:
        logger.warning(f"No writable fields for file {file_id} — skipping list write")
        return "skipped"

    existing = _find_item_by_file_id(file_id)

    if existing:
        item_id = existing["id"]
        cells = [{**f, "row_id": item_id} for f in fields]
        _slack_post("slackLists.items.update", {
            "list_id": LIST_ID,
            "cells": cells,
        })
        logger.info(f"Updated Slack list item {item_id} for file {file_id}")
        return "updated"
    else:
        _slack_post("slackLists.items.create", {
            "list_id": LIST_ID,
            "initial_fields": fields,
        })
        logger.info(f"Created new Slack list item for file {file_id}")
        return "created"
