#!/usr/bin/env python3
"""
Sync a fixed set of env vars from GitHub Actions secrets into a Vercel
project's Production environment variables via the Vercel REST API.

Reads values from the process environment (the workflow maps each GitHub
secret onto an identically-named env var before invoking this script), then
for each var:
  - looks up whether it already exists in the target Vercel project/env
  - PATCHes it if it exists, POSTs a new one if it doesn't
  - skips (with a warning) any var whose value is empty/unset, so a missing
    GitHub secret doesn't wipe out a value already set in Vercel

Requires (as process env vars):
  VERCEL_TOKEN        - Vercel API token (Account Settings -> Tokens)
  VERCEL_PROJECT_ID   - from `vercel link` -> .vercel/project.json, or the
                        Vercel dashboard project settings page
  VERCEL_TEAM_ID      - optional, only needed if the project lives under a
                        team/org rather than a personal account

No third-party dependencies -- stdlib only (urllib), so nothing needs to be
added to requirements.txt.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.vercel.com"

# GitHub secret name == Vercel env var name == key in the workflow's `env:` block.
# Add/remove entries here as the app's env surface changes.
ENV_VARS = [
    "FRAMEIO_SIGNING_SECRET",
    "FRAMEIO_ACCOUNT_ID",
    "ADOBE_CLIENT_ID",
    "ADOBE_CLIENT_SECRET",
    "ADOBE_REFRESH_TOKEN",
    "OAUTH_CALLBACK_ENABLED",
    "SHEET_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "SHEETS_ENABLED",
]

# Which Vercel environment(s) these get written to.
TARGET = ["production"]


def require_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def api(token, method, path, team_id, body=None):
    url = f"{API_BASE}{path}"
    if team_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}teamId={team_id}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def fetch_existing(token, project_id, team_id):
    status, body = api(token, "GET", f"/v9/projects/{project_id}/env", team_id)
    if status != 200:
        print(f"ERROR: failed to list existing env vars: {status} {body}", file=sys.stderr)
        sys.exit(1)
    return {e["key"]: e for e in body.get("envs", [])}


def upsert(token, project_id, team_id, key, value, existing):
    if not value:
        print(f"skip {key}: no value provided (leaving Vercel's current value untouched)")
        return

    match = existing.get(key)
    if match and any(t in match.get("target", []) for t in TARGET):
        status, body = api(
            token, "PATCH", f"/v9/projects/{project_id}/env/{match['id']}", team_id,
            {"value": value},
        )
        action = "updated"
    else:
        status, body = api(
            token, "POST", f"/v10/projects/{project_id}/env", team_id,
            {"key": key, "value": value, "type": "encrypted", "target": TARGET},
        )
        action = "created"

    if status not in (200, 201):
        print(f"ERROR: failed to sync {key}: {status} {body}", file=sys.stderr)
        sys.exit(1)

    print(f"{action} {key} ({'/'.join(TARGET)})")


def main():
    token = require_env("VERCEL_TOKEN")
    project_id = require_env("VERCEL_PROJECT_ID")
    team_id = os.environ.get("VERCEL_TEAM_ID") or None

    existing = fetch_existing(token, project_id, team_id)

    for key in ENV_VARS:
        upsert(token, project_id, team_id, key, os.environ.get(key, ""), existing)


if __name__ == "__main__":
    main()