#!/usr/bin/env python3
"""
Rotate the Adobe IMS refresh token this app uses (see frameio_client.py).

Adobe's IMS token endpoint revokes a refresh token the instant it's used and
issues a brand-new one in the same response (this app's own
`get_access_token()` already detects that but only logs it -- this script is
what actually persists it). Flow:

  1. Read the CURRENT refresh token out of a file `vercel env pull` already
     wrote (so this refreshes whatever is actually live in Vercel right now,
     not a possibly-stale copy).
  2. Exchange it with Adobe for a new access + refresh token pair.
  3. If Adobe issued a new refresh token, upsert it into Vercel's Production
     env (same PATCH-if-exists/POST-if-not pattern as sync_vercel_env.py) and
     write it to $GITHUB_OUTPUT so the workflow can also push it to the
     GitHub secret, keeping the two in sync for next time.

Note: this only guards against the token going stale from disuse between
scheduled runs. If the live app itself rotates the token independently
between runs (it can -- see frameio_client.py) and only logs the new value,
the copy in Vercel is already stale and this script's Adobe call will fail
with invalid_grant. That failure is loud (non-zero exit), not silent -- when
it happens, the one-time OAuth consent flow in app.py's /oauth/start needs to
be re-run by hand.

Requires as process env vars:
  ADOBE_CLIENT_ID, ADOBE_CLIENT_SECRET  - static Adobe OAuth app credentials
  VERCEL_TOKEN, VERCEL_PROJECT_ID       - to write the new token to Vercel
  VERCEL_TEAM_ID                        - optional, only for team projects

Reads the current refresh token from CURRENT_ENV_FILE (default
".vercel-current.env", produced by `vercel env pull` in the workflow).

No third-party dependencies -- stdlib only (urllib).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ADOBE_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
VERCEL_API_BASE = "https://api.vercel.com"
TARGET = ["production"]
ENV_VAR_NAME = "ADOBE_REFRESH_TOKEN"
CURRENT_ENV_FILE = os.environ.get("CURRENT_ENV_FILE", ".vercel-current.env")


def require_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return value


def read_current_refresh_token(path):
    if not os.path.exists(path):
        print(f"ERROR: {path} not found -- did `vercel env pull` run first?", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == ENV_VAR_NAME:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                return value
    print(f"ERROR: {ENV_VAR_NAME} not present in {path}", file=sys.stderr)
    sys.exit(1)


def refresh_with_adobe(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(ADOBE_IMS_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: Adobe token refresh failed: {e.code} {body}", file=sys.stderr)
        sys.exit(1)


def vercel_api(token, method, path, team_id, body=None):
    url = f"{VERCEL_API_BASE}{path}"
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


def update_vercel(vercel_token, project_id, team_id, new_value):
    status, body = vercel_api(vercel_token, "GET", f"/v9/projects/{project_id}/env", team_id)
    if status != 200:
        print(f"ERROR: failed to list Vercel env vars: {status} {body}", file=sys.stderr)
        sys.exit(1)
    existing = {e["key"]: e for e in body.get("envs", [])}
    match = existing.get(ENV_VAR_NAME)

    if match and any(t in match.get("target", []) for t in TARGET):
        status, body = vercel_api(
            vercel_token, "PATCH", f"/v9/projects/{project_id}/env/{match['id']}", team_id,
            {"value": new_value},
        )
    else:
        status, body = vercel_api(
            vercel_token, "POST", f"/v10/projects/{project_id}/env", team_id,
            {"key": ENV_VAR_NAME, "value": new_value, "type": "encrypted", "target": TARGET},
        )
    if status not in (200, 201):
        print(f"ERROR: failed to update Vercel env var: {status} {body}", file=sys.stderr)
        sys.exit(1)
    print(f"Updated {ENV_VAR_NAME} in Vercel (production)")


def write_output(**kwargs):
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a") as f:
        for key, value in kwargs.items():
            f.write(f"{key}={value}\n")


def main():
    client_id = require_env("ADOBE_CLIENT_ID")
    client_secret = require_env("ADOBE_CLIENT_SECRET")
    vercel_token = require_env("VERCEL_TOKEN")
    project_id = require_env("VERCEL_PROJECT_ID")
    team_id = os.environ.get("VERCEL_TEAM_ID") or None

    current_refresh_token = read_current_refresh_token(CURRENT_ENV_FILE)

    tokens = refresh_with_adobe(client_id, client_secret, current_refresh_token)
    new_refresh_token = tokens.get("refresh_token")

    if not new_refresh_token:
        print(
            "ERROR: Adobe did not return a refresh_token in the response. "
            "The original token grant may be missing the offline_access scope.",
            file=sys.stderr,
        )
        sys.exit(1)

    if new_refresh_token == current_refresh_token:
        print("Adobe returned the same refresh token -- nothing to update.")
        write_output(rotated="false")
        return

    update_vercel(vercel_token, project_id, team_id, new_refresh_token)
    write_output(rotated="true", new_token=new_refresh_token)
    print("Rotation complete: new token written to Vercel; passed to the workflow to update the GitHub secret.")


if __name__ == "__main__":
    main()