"""
One-time helper to discover Slack List column IDs and select option IDs.

Uses slackLists.items.info (single-item GET) which returns the full list schema
with human-readable column names and option labels — no manual matching required.

Usage:
  1. Ensure SLACK_BOT_TOKEN and SLACK_LIST_ID are set in your environment (or .env).
  2. Add at least one item to the list in Slack (all select columns filled in helps).
  3. Run:  python discover_schema.py
  4. Copy the printed .env block into your .env file and Vercel env vars.
"""

import os
import sys
import json
import requests

SLACK_API = "https://slack.com/api"

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
LIST_ID = os.environ.get("SLACK_LIST_ID", "")

if not TOKEN or not LIST_ID:
    print("ERROR: SLACK_BOT_TOKEN and SLACK_LIST_ID must be set in the environment.")
    sys.exit(1)


def slack_get(method: str, params: dict) -> dict:
    resp = requests.get(
        f"{SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error ({method}): {data.get('error')}")
    return data


def fetch_schema(list_id: str) -> dict:
    """Fetch the full list schema (with column names and option labels) via items.info."""
    # Step 1: get one item ID from items.list
    data = slack_get("slackLists.items.list", {"list_id": list_id, "limit": 1})
    items = data.get("items", [])
    if not items:
        print("No items found in the list.")
        print("Add at least one item via the Slack UI, then re-run this script.")
        sys.exit(1)

    item_id = items[0].get("id")
    if not item_id:
        print("ERROR: Could not extract item ID from items.list response.")
        sys.exit(1)

    print(f"Using item {item_id} to fetch full list schema...\n")

    # Step 2: call items.info — response contains list object with list_metadata.schema
    info = slack_get("slackLists.items.info", {"list_id": list_id, "id": item_id})

    list_data = info.get("list", {})

    print("=" * 60)
    print("RAW JSON OF list (for debugging):")
    print("=" * 60)
    print(json.dumps(list_data, indent=2))
    print()

    return list_data


def parse_schema(list_data: dict) -> dict:
    """Parse list_metadata.schema into: col_id -> {name, type, choices: {opt_id -> label}}."""
    # Schema lives under list_metadata.schema (not top-level schema)
    schema = list_data.get("list_metadata", {}).get("schema", [])
    columns = {}
    for col in schema:
        col_id = col.get("id")
        if not col_id:
            continue
        choices = {}
        if col.get("type") == "select":
            for choice in col.get("options", {}).get("choices", []):
                opt_id = choice.get("value", "")
                label = choice.get("label", opt_id)
                if opt_id:
                    choices[opt_id] = label
        columns[col_id] = {
            "name": col.get("name", ""),
            "type": col.get("type", "unknown"),
            "choices": choices,
        }
    return columns


# Column name -> env var name (case-insensitive matching)
COL_NAME_MAP = {
    "name": "SLACK_COL_NAME",
    "production id": "SLACK_COL_NAME",
    "file id": "SLACK_COL_FILE_ID",
    "fileid": "SLACK_COL_FILE_ID",
    "sme": "SLACK_COL_SME",
    "pm": "SLACK_COL_PM",
    "status": "SLACK_COL_STATUS",
    "notes": "SLACK_COL_NOTES",
}

# Option label -> env var name for SME column (case-insensitive)
SME_OPT_MAP = {
    "needs review": "SLACK_SME_OPT_NEEDS_REVIEW",
    "in progress":  "SLACK_SME_OPT_IN_PROGRESS",
    "approved":     "SLACK_SME_OPT_APPROVED",
    "n/a":          "SLACK_SME_OPT_NA",
}

# Option label -> env var name for PM column (case-insensitive)
PM_OPT_MAP = {
    "needs review": "SLACK_PM_OPT_NEEDS_REVIEW",
    "in progress":  "SLACK_PM_OPT_IN_PROGRESS",
    "approved":     "SLACK_PM_OPT_APPROVED",
    "n/a":          "SLACK_PM_OPT_NA",
}

# Option label -> env var name for Status column (case-insensitive)
STATUS_OPT_MAP = {
    "rough cut ready":      "SLACK_STATUS_OPT_ROUGH_CUT_READY",
    "r1 comments":          "SLACK_STATUS_OPT_R1_COMMENTS",
    "r2 comments":          "SLACK_STATUS_OPT_R2_COMMENTS",
    "r2 edits":             "SLACK_STATUS_OPT_R2_EDITS",
    "approvals":            "SLACK_STATUS_OPT_APPROVALS",
    "full length lecture":  "SLACK_STATUS_OPT_FULL_LENGTH_LECTURE",
}


def main():
    print("=" * 60)
    print("WARNING: Slack omits empty fields from API responses.")
    print("For best results, ensure at least one list item exists")
    print("with all select columns filled in before running.")
    print("=" * 60 + "\n")

    list_data = fetch_schema(LIST_ID)
    columns = parse_schema(list_data)

    if not columns:
        print("ERROR: No schema columns found in list data.")
        print("Expected list_metadata.schema to be non-empty. Check the raw JSON above.")
        sys.exit(1)

    # --- Auto-match columns to env vars ---
    col_env: dict[str, str] = {}  # col_id -> env var name
    for col_id, info in columns.items():
        key = info["name"].strip().lower()
        env_var = COL_NAME_MAP.get(key)
        if env_var:
            col_env[col_id] = env_var

    # --- Auto-match option IDs to env vars (SME and PM tracked separately) ---
    opt_env: dict[str, str] = {}  # opt_id -> env var name

    sme_col_id = next((cid for cid, ev in col_env.items() if ev == "SLACK_COL_SME"), None)
    pm_col_id = next((cid for cid, ev in col_env.items() if ev == "SLACK_COL_PM"), None)
    status_col_id = next((cid for cid, ev in col_env.items() if ev == "SLACK_COL_STATUS"), None)

    if sme_col_id:
        for opt_id, label in columns[sme_col_id]["choices"].items():
            env_var = SME_OPT_MAP.get(label.strip().lower())
            if env_var:
                opt_env[opt_id] = env_var

    if pm_col_id:
        for opt_id, label in columns[pm_col_id]["choices"].items():
            env_var = PM_OPT_MAP.get(label.strip().lower())
            if env_var:
                opt_env[opt_id] = env_var

    if status_col_id:
        for opt_id, label in columns[status_col_id]["choices"].items():
            env_var = STATUS_OPT_MAP.get(label.strip().lower())
            if env_var:
                opt_env[opt_id] = env_var

    # --- Print all discovered columns ---
    print("=" * 60)
    print("DISCOVERED COLUMNS:")
    print("=" * 60)
    for col_id, info in columns.items():
        matched = col_env.get(col_id, "# UNMATCHED")
        print(f"\n  {col_id}  →  {matched}")
        print(f"    name: {info['name']!r}   type: {info['type']}")
        if info["choices"]:
            for opt_id, label in info["choices"].items():
                opt_matched = opt_env.get(opt_id, "# UNMATCHED")
                print(f"    option  {opt_id}  →  {opt_matched}  ({label!r})")

    # --- Build pre-filled env snippet ---
    env_col: dict[str, str] = {v: k for k, v in col_env.items()}
    env_opt: dict[str, str] = {v: k for k, v in opt_env.items()}

    def col_val(env_var: str) -> str:
        return env_col.get(env_var, "")

    def opt_val(env_var: str) -> str:
        return env_opt.get(env_var, "")

    def line(env_var: str, value: str) -> str:
        if value:
            return f"{env_var}={value}"
        return f"{env_var}=  # UNMATCHED"

    print("\n" + "=" * 60)
    print("SUGGESTED .env SNIPPET  (copy into your .env file)")
    print("=" * 60 + "\n")

    print(line("SLACK_COL_NAME",    col_val("SLACK_COL_NAME")))
    print(line("SLACK_COL_FILE_ID", col_val("SLACK_COL_FILE_ID")))
    print(line("SLACK_COL_SME",     col_val("SLACK_COL_SME")))
    print(line("SLACK_COL_PM",      col_val("SLACK_COL_PM")))
    print(line("SLACK_COL_STATUS",  col_val("SLACK_COL_STATUS")))
    print(line("SLACK_COL_NOTES",   col_val("SLACK_COL_NOTES")))
    print()
    print("# SME column option IDs")
    print(line("SLACK_SME_OPT_NEEDS_REVIEW", opt_val("SLACK_SME_OPT_NEEDS_REVIEW")))
    print(line("SLACK_SME_OPT_IN_PROGRESS",  opt_val("SLACK_SME_OPT_IN_PROGRESS")))
    print(line("SLACK_SME_OPT_APPROVED",     opt_val("SLACK_SME_OPT_APPROVED")))
    print(line("SLACK_SME_OPT_NA",           opt_val("SLACK_SME_OPT_NA")))
    print()
    print("# PM column option IDs")
    print(line("SLACK_PM_OPT_NEEDS_REVIEW", opt_val("SLACK_PM_OPT_NEEDS_REVIEW")))
    print(line("SLACK_PM_OPT_IN_PROGRESS",  opt_val("SLACK_PM_OPT_IN_PROGRESS")))
    print(line("SLACK_PM_OPT_APPROVED",     opt_val("SLACK_PM_OPT_APPROVED")))
    print(line("SLACK_PM_OPT_NA",           opt_val("SLACK_PM_OPT_NA")))
    print()
    print("# Status column option IDs")
    print(line("SLACK_STATUS_OPT_ROUGH_CUT_READY",    opt_val("SLACK_STATUS_OPT_ROUGH_CUT_READY")))
    print(line("SLACK_STATUS_OPT_R1_COMMENTS",         opt_val("SLACK_STATUS_OPT_R1_COMMENTS")))
    print(line("SLACK_STATUS_OPT_R2_COMMENTS",         opt_val("SLACK_STATUS_OPT_R2_COMMENTS")))
    print(line("SLACK_STATUS_OPT_R2_EDITS",            opt_val("SLACK_STATUS_OPT_R2_EDITS")))
    print(line("SLACK_STATUS_OPT_APPROVALS",           opt_val("SLACK_STATUS_OPT_APPROVALS")))
    print(line("SLACK_STATUS_OPT_FULL_LENGTH_LECTURE", opt_val("SLACK_STATUS_OPT_FULL_LENGTH_LECTURE")))


if __name__ == "__main__":
    main()
