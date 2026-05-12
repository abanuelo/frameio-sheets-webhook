# Frame.io → Slack List Webhook

Automatically syncs Frame.io asset activity to a Slack List in a private channel. When a new file is uploaded it appears as a new row. When metadata fields on an existing asset change, the corresponding row is updated in place.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Environment Variables](#environment-variables)
3. [Setup Guide](#setup-guide)
   - [Deploy to Vercel](#1-deploy-to-vercel)
   - [Configure the Frame.io Webhook](#2-configure-the-frameio-webhook)
   - [Adobe Developer Console (Frame.io API)](#3-adobe-developer-console-frameio-api)
   - [Slack App Setup](#4-slack-app-setup)
   - [Discover Column and Option IDs](#5-discover-column-and-option-ids)
4. [List Structure](#list-structure)
5. [Metadata Field Names Must Match Frame.io Exactly](#metadata-field-names-must-match-frameio-exactly)

---

## How It Works

```
Frame.io event
      │
      ▼
POST /api/webhook          ← app.py verifies HMAC signature
      │
      └─► handle_event()       ← enrichment.py
               │
               ├─ Skips events not in ENRICHMENT_EVENTS list
               │   (file.created, file.ready, file.label.updated, metadata.value.updated)
               │
               ├─ Fetches full file data from Frame.io API (includes metadata fields)
               │
               ├─ Maps Frame.io metadata field names → Slack list column keys
               │
               └─► upsert_list_item()   ← slack_writer.py
                        │
                        ├─ Searches list for matching Frame.io File ID
                        ├─ Found ──────────────────────────────► UPDATE row
                        └─ Not found ──────────────────────────► INSERT new row
```

### New Asset Uploaded

When a file is uploaded to Frame.io a `file.created` or `file.ready` event fires. The webhook fetches the full file record and, if no row exists yet for that file ID, **creates a new row** in the Slack list with all available metadata.

### Metadata Field Changed

When someone edits a custom metadata field on an existing asset a `metadata.value.updated` event fires. The webhook fetches the updated file, locates the existing row by Frame.io File ID, and **updates only the changed cells**. Fields not managed by Frame.io are left untouched.

---

## Environment Variables

> [!IMPORTANT]
> All variables below are required in production. Add them under **Vercel → Project Settings → Environment Variables → Production** before deploying. A missing variable will cause the app to crash on startup.

### Frame.io / Adobe

| Variable | Where to get it | Notes |
|---|---|---|
| `FRAMEIO_SIGNING_SECRET` | Frame.io → Settings → Webhooks, shown once at webhook creation | Used to verify every inbound webhook payload |
| `FRAMEIO_ACCOUNT_ID` | Frame.io URL: `next.frame.io/?a=<this value>` | Required to call the Frame.io v4 API |
| `ADOBE_CLIENT_ID` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_CLIENT_SECRET` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_REFRESH_TOKEN` | Captured via the one-time `/oauth/callback` flow (see below) | Long-lived token; rotate if Adobe warns you it changed |
| `OAUTH_CALLBACK_ENABLED` | Set manually | `true` only during the one-time OAuth setup, then set to `false` |

### Slack Lists

| Variable | Where to get it | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | Slack API → OAuth & Permissions → Bot User OAuth Token | Starts with `xoxb-` |
| `SLACK_LIST_ID` | The final segment of the Slack list URL | e.g. `F0B2ZR12X43` |
| `SLACK_COL_NAME` | Run `discover_schema.py` | Column ID for the Name (Production ID) column |
| `SLACK_COL_FILE_ID` | Run `discover_schema.py` | Column ID for the Frame.io File ID column |
| `SLACK_COL_SME` | Run `discover_schema.py` | Column ID for the SME column |
| `SLACK_COL_PM` | Run `discover_schema.py` | Column ID for the PM column |
| `SLACK_COL_STATUS` | Run `discover_schema.py` | Column ID for the Status column |
| `SLACK_COL_NOTES` | Run `discover_schema.py` | Column ID for the Notes column |
| `SLACK_OPT_NEEDS_REVIEW` | Run `discover_schema.py` | Option ID for "Needs Review" (SME/PM) |
| `SLACK_OPT_IN_PROGRESS` | Run `discover_schema.py` | Option ID for "In Progress" (SME/PM) |
| `SLACK_OPT_APPROVED` | Run `discover_schema.py` | Option ID for "Approved" (SME/PM) |
| `SLACK_OPT_NA` | Run `discover_schema.py` | Option ID for "N/A" (SME/PM) |
| `SLACK_STATUS_OPT_ROUGH_CUT_READY` | Run `discover_schema.py` | Option ID for "Rough Cut Ready" (Status) |
| `SLACK_STATUS_OPT_R1_COMMENTS` | Run `discover_schema.py` | Option ID for "R1 Comments" (Status) |
| `SLACK_STATUS_OPT_R2_COMMENTS` | Run `discover_schema.py` | Option ID for "R2 Comments" (Status) |
| `SLACK_STATUS_OPT_R2_EDITS` | Run `discover_schema.py` | Option ID for "R2 Edits" (Status) |
| `SLACK_STATUS_OPT_APPROVALS` | Run `discover_schema.py` | Option ID for "Approvals" (Status) |
| `SLACK_STATUS_OPT_FULL_LENGTH_LECTURE` | Run `discover_schema.py` | Option ID for "Full Length Lecture" (Status) |

> [!NOTE]
> `ADOBE_REFRESH_TOKEN` can rotate. If the Frame.io API starts returning 401 errors, check Vercel logs — the app will log a warning with the new token value. Update the env var and redeploy.

---

## Setup Guide

### 1. Deploy to Vercel

Navigate to [Vercel](https://vercel.com/) and create a new project. Import from GitHub under **Import Git Repository**. Deploy with all defaults — the only required setting is selecting **Python** as the **Application Preset**.

After deployment you will have:
- App URL: `<repo-name>.vercel.app`
- Webhook endpoint: `<repo-name>.vercel.app/api/webhook`
- Health check: `<repo-name>.vercel.app/health`

### 2. Configure the Frame.io Webhook

Navigate to [next.frame.io/settings/webhooks](https://next.frame.io/settings/webhooks) and click **+ New Webhook**.

- **Webhook URL:** your Vercel endpoint (`<repo-name>.vercel.app/api/webhook`)
- **Events:** select all events

> [!IMPORTANT]
> When you click **Create**, Frame.io shows the webhook secret **once**. Copy it immediately and save it as `FRAMEIO_SIGNING_SECRET` in Vercel.

Also note the account ID from the URL (`?a=<large numbers>`) and save it as `FRAMEIO_ACCOUNT_ID`.

### 3. Adobe Developer Console (Frame.io API)

Navigate to the [Adobe Developer Console](https://developer.adobe.com/console) → **API and Services** → search for **Frame.io** → **Create Project** → **User Authentication** → **OAuth** → **OAuth Web App**.

Configure the OAuth app:
- **Default redirect URI:** `https://<repo-name>.vercel.app/oauth/callback`
- **Redirect URI pattern:** `https://<repo-name>\.vercel\.app/oauth/callback`

You will receive a **client_id** and **client_secret** — save these as `ADOBE_CLIENT_ID` and `ADOBE_CLIENT_SECRET` in Vercel.

**One-time OAuth token capture:**

1. Add `OAUTH_CALLBACK_ENABLED=true` to Vercel env vars and redeploy.
2. Visit this URL in a browser (replace placeholders):
   ```
   https://ims-na1.adobelogin.com/ims/authorize/v2?client_id=<ADOBE_CLIENT_ID>&scope=openid,AdobeID,offline_access,additional_info.roles,email,profile&response_type=code&redirect_uri=https://<VERCEL-APP-DOMAIN>/oauth/callback
   ```
3. Complete the sign-in flow. The page will display your refresh token.
4. Save it as `ADOBE_REFRESH_TOKEN` in Vercel.
5. Set `OAUTH_CALLBACK_ENABLED=false` (or remove it) and redeploy.

> [!IMPORTANT]
> Leaving `OAUTH_CALLBACK_ENABLED=true` is a security risk — anyone who visits the callback URL could capture a new token. Always disable it after the one-time setup.

Lastly, on your [Frame.io profile settings](https://next.frame.io/settings/profile) click **Manage on Adobe** to confirm the Adobe Developer app is linked to your Frame.io account.

### 4. Slack App Setup

**Create the app:**

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch**.
2. Name it (e.g. `FrameIO Webhook`) and select your workspace.

**Add OAuth scopes** (OAuth & Permissions → Bot Token Scopes):

| Scope | Purpose |
|---|---|
| `lists:read` | Read list items to search for existing rows |
| `lists:write` | Create and update list rows |
| `groups:read` | Access the private channel where the list lives |

3. Click **Install to Workspace** and authorize.
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`) → save as `SLACK_BOT_TOKEN`.

**Add the bot to the private channel:**

5. In Slack, open the private channel where your list lives.
6. Click the channel name → **Integrations** tab → **Add apps** → search for your app name.

**Get the List ID:**

7. Open the list in Slack. The List ID is the last segment of the URL:
   `https://<workspace>.slack.com/lists/<team-id>/<list-id>`
   Save it as `SLACK_LIST_ID`.

> [!NOTE]
> Slack Lists require a **paid Slack workspace**. They are not available on the free plan.

### 5. Discover Column and Option IDs

The Slack Lists API requires opaque column IDs and select option IDs — not human-readable names. Run the provided helper script once to discover these values.

**Prerequisites:** At least one item must exist in the list with all select columns filled in so all option IDs are visible. Add a test row manually via the Slack UI if needed.

```bash
# Set env vars first (or use a .env file with python-dotenv)
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_LIST_ID=F0B2ZR12X43

python discover_schema.py
```

The script will print:
1. A raw dump of every column ID it found and sample values / option names
2. A suggested `.env` snippet with instructions for mapping each column

Copy the column IDs and option IDs into your `.env` file and Vercel env vars, then redeploy.

---

## List Structure

The Slack list has 6 columns, all populated automatically by the webhook:

| Column | Type | Frame.io Source | Select Options |
|---|---|---|---|
| Name | text | Asset filename | — |
| File ID | text | Asset ID | — |
| SME | select | `SME` metadata field | Needs Review, In Progress, Approved, N/A |
| PM | select | `PM` metadata field | Needs Review, In Progress, Approved, N/A |
| Status | select | `Overall Video Status` metadata field | Rough Cut Ready, R1 Comments, R2 Comments, R2 Edits, Approvals, Full Length Lecture |
| Notes | text | `Notes` metadata field | — |

The lookup key is **File ID** — every upsert first searches the list for a row with a matching Frame.io file ID before deciding whether to create or update.

---

## Metadata Field Names Must Match Frame.io Exactly

> [!IMPORTANT]
> The field names in `METADATA_FIELD_MAP` (`enrichment.py`) are matched against the `field_definition_name` returned by the Frame.io API. They are **case-sensitive and must match exactly** what is configured in your Frame.io account's metadata schema.

```python
# enrichment.py
METADATA_FIELD_MAP = {
    'Overall Video Status': 'status',
    'PM':                   'pm',
    'SME':                  'sme',
    'Notes':                'notes',
    'Production ID':        'production_id',
}
```

If a field name drifts (e.g. renamed from `"PM"` to `"Project Manager"`) the mapping will silently stop syncing that field — update the key here to match.
