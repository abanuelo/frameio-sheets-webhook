# Frame.io → Google Sheets Webhook

Automatically syncs Frame.io asset activity to a Google Sheet. When a new file is uploaded it appears as a new row. When metadata fields on an existing asset change, the corresponding row is updated in place.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Environment Variables](#environment-variables)
3. [Setup Guide](#setup-guide)
   - [Deploy to Vercel](#1-deploy-to-vercel)
   - [Configure the Frame.io Webhook](#2-configure-the-frameio-webhook)
   - [Adobe Developer Console (Frame.io API)](#3-adobe-developer-console-frameio-api)
   - [Google Service Account](#4-google-service-account)
4. [Sheet Structure](#sheet-structure)
5. [Customizing the Sheet Columns](#customizing-the-sheet-columns)
6. [Metadata Field Names Must Match Frame.io Exactly](#metadata-field-names-must-match-frameio-exactly)

---

## How It Works

```
Frame.io event
      │
      ▼
POST /api/webhook          ← app.py verifies HMAC signature
      │
      ├─► append_event_row()   ← always writes to "webhook events" tab (raw log)
      │
      └─► handle_event()       ← enrichment.py
               │
               ├─ Skips events not in ENRICHMENT_EVENTS list
               │   (file.created, file.ready, file.label.updated, metadata.value.updated)
               │
               ├─ Fetches full file data from Frame.io API (includes metadata fields)
               │
               ├─ Maps Frame.io metadata field names → sheet column keys
               │
               └─► upsert_project_row()   ← sheets_writer.py
                        │
                        ├─ Looks up row by Production ID  ──► found → UPDATE cells
                        ├─ Falls back to Frame.io File ID ──► found → UPDATE cells
                        └─ No match ─────────────────────────────► INSERT new row
```

### New Asset Uploaded

When a file is uploaded to Frame.io a `file.created` or `file.ready` event fires. The webhook fetches the full file record, finds the matching Google Sheet tab by **project name**, and — if no row exists yet for that file — **appends a new row** with all available metadata.

### Metadata Field Changed

When someone edits a custom metadata field on an existing asset a `metadata.value.updated` event fires. The webhook fetches the updated file, locates the existing row (first by Production ID, then by Frame.io File ID), and **updates only the changed cells** — blank values are never written back, so manually managed columns (Speaker, Release, Editor) are preserved.

---

## Environment Variables

> [!IMPORTANT]
> All variables below are required in production. Add them under **Vercel → Project Settings → Environment Variables → Production** before deploying. A missing variable will cause the app to crash on startup.

| Variable | Where to get it | Notes |
|---|---|---|
| `FRAMEIO_SIGNING_SECRET` | Frame.io → Settings → Webhooks, shown once at webhook creation | Used to verify every inbound webhook payload |
| `FRAMEIO_ACCOUNT_ID` | Frame.io URL: `next.frame.io/?a=<this value>` | Required to call the Frame.io v4 API |
| `ADOBE_CLIENT_ID` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_CLIENT_SECRET` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_REFRESH_TOKEN` | Captured via the one-time `/oauth/callback` flow (see below) | Long-lived token; rotate if Adobe warns you it changed |
| `OAUTH_CALLBACK_ENABLED` | Set manually | `true` only during the one-time OAuth setup, then set to `false` |
| `SHEET_ID` | Google Sheets URL: `…/d/<this value>/edit` | ID of the target spreadsheet |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | GCP Console → Service Accounts → Keys → JSON | Paste the **entire JSON file contents** as the value |

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

### 4. Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Under **APIs & Services**, enable the **Google Sheets API**.
3. Go to **IAM & Admin → Service Accounts** → **Create service account**.
4. Grant the service account the **Owner** role (or at minimum Editor).
5. Share your target Google Sheet with the service account email (`<name>@<project>.iam.gserviceaccount.com`) as **Editor**.
6. Get the Sheet ID from the URL (`…/d/<sheet-id>/edit`) and save it as `SHEET_ID`.
7. On the service account row, go to **Keys → Add Key → Create new key → JSON**. A file downloads — paste its entire contents as `GOOGLE_SERVICE_ACCOUNT_JSON` in Vercel.

Redeploy after adding these variables.

---

## Sheet Structure

Here is the [sheet template](https://docs.google.com/spreadsheets/d/12UlLo53JBk9GBMfgaz0-QzQyEAPchjoyN10nXYP9Ma4/edit?usp=sharing).

> [!IMPORTANT]
> **Project tab names must exactly match the Frame.io project name.** The app looks up the tab by calling `project.name` from the Frame.io API response. If there is no matching tab, the event is silently skipped.

The spreadsheet has two kinds of tabs:

**Project tabs** (one per Frame.io project, name must match exactly):

| Column | Letter | Field | Managed by |
|---|---|---|---|
| Status | A | `Overall Video Status` metadata field | Frame.io |
| Frame.io File ID | B | Asset ID from Frame.io | Automatic |
| Production ID | C | Asset filename from Frame.io | Automatic |
| Name | D | *(currently unused — reserved)* | Manual |
| Release | E | Release label | Manual |
| Speaker | F | Speaker name | Manual |
| SME | G | `SME` metadata field | Frame.io |
| PM | H | `PM` metadata field | Frame.io |
| Notes | I | `Notes` metadata field | Frame.io |
| Editor | J | Editor name | Manual |

**webhook events tab** (append-only raw log):

| Column | Field |
|---|---|
| A | Event type |
| B | Timestamp (UTC ISO-8601) |
| C | Raw JSON payload (truncated at 50,000 chars) |

> [!NOTE]
> Columns marked **Manual** are never overwritten by the webhook. The upsert logic skips any key whose value is blank or `None`, so manually entered values are always preserved.

---

## Customizing the Sheet Columns

If you need to add, remove, or reorder columns, update `sheets_writer.py`:

**Column letter assignments** (`sheets_writer.py` lines 17–26):
```python
COL_STATUS         = 'A'
COL_FRAMEIO_FILE_ID = 'B'
COL_PRODUCTION_ID  = 'C'
COL_NAME           = 'D'
COL_RELEASE        = 'E'
COL_SPEAKER        = 'F'
COL_SME            = 'G'
COL_PM             = 'H'
COL_NOTES          = 'I'
COL_EDITOR         = 'J'
```

**Field → column letter mapping** (`sheets_writer.py` lines 29–40):
```python
SHEET_COLUMNS = {
    'status':           COL_STATUS,
    'frameio_file_id':  COL_FRAMEIO_FILE_ID,
    'production_id':    COL_PRODUCTION_ID,
    ...
}
```

**Insert column order** (`sheets_writer.py` lines 43–44) — must match the physical column order A through J (or however many you have):
```python
COLUMN_ORDER = ['status', 'frameio_file_id', 'production_id', 'name',
                'release', 'speaker', 'sme', 'pm', 'notes', 'editor']
```

If you add a new column `K`, add a new `COL_*` constant, add it to `SHEET_COLUMNS`, and append it to `COLUMN_ORDER`.

---

## Metadata Field Names Must Match Frame.io Exactly

> [!IMPORTANT]
> The field names in `METADATA_FIELD_TO_SHEET_KEY` (`enrichment.py` lines 15–21) are matched against the `field_definition_name` returned by the Frame.io API. They are **case-sensitive and must match exactly** what is configured in your Frame.io account's metadata schema.

```python
# enrichment.py lines 15–21
METADATA_FIELD_TO_SHEET_KEY = {
    'Overall Video Status': 'status',
    'PM':                   'pm',
    'SME':                  'sme',
    'Notes':                'notes',
    'Production ID':        'production_id',
}
```

To verify the exact field names, check the `field_definition_name` values in the raw JSON logged to the **webhook events** tab, or inspect the Frame.io metadata schema under your account settings. If a field name drifts (e.g. renamed from `"PM"` to `"Project Manager"`) the mapping will silently stop syncing that field — update the key here to match.
