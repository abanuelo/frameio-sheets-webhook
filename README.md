# Frame.io → Google Sheets Webhook

Automatically syncs Frame.io asset activity to a Google Sheet. When a new file is uploaded it appears as a new row. When metadata fields on an existing asset change, the corresponding row is updated in place.

> [!NOTE]
> An Airtable integration is also bundled (in `airtable_writer.py`) but is **disabled by default**. Google Sheets is the active backend. See [Re-enabling Airtable](#re-enabling-airtable) to switch it back on. The two backends are independent — either or both can run at once.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Environment Variables](#environment-variables)
3. [Setup Guide](#setup-guide)
   - [Deploy to Vercel](#1-deploy-to-vercel)
   - [Configure the Frame.io Webhook](#2-configure-the-frameio-webhook)
   - [Adobe Developer Console (Frame.io API)](#3-adobe-developer-console-frameio-api)
   - [Google Sheets Setup](#4-google-sheets-setup)
   - [Verify the Integration](#5-verify-the-integration)
4. [Sheet Structure](#sheet-structure)
5. [Metadata Field Names](#metadata-field-names)
6. [Re-enabling Airtable](#re-enabling-airtable)

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
               ├─ Maps Frame.io metadata field names → internal keys (METADATA_FIELD_MAP)
               │
               └─► upsert_record()   ← sheets_writer.py (and/or airtable_writer.py)
                        │
                        ├─ Picks the tab whose name matches the asset's Frame.io project (case-insensitive)
                        ├─ Searches the tab for a matching Frame.io File ID
                        ├─ Found ──────────────────────────────► UPDATE row
                        └─ Not found ──────────────────────────► INSERT new row
```

Routing is driven by the `SHEETS_ENABLED` / `AIRTABLE_ENABLED` flags. By default only Sheets runs.

### New Asset Uploaded

When a file is uploaded to Frame.io a `file.created` or `file.ready` event fires. The webhook fetches the full file record and, if no row exists yet for that file ID, **creates a new row** in the matching tab with all available metadata.

### Metadata Field Changed

When someone edits a custom metadata field on an existing asset a `metadata.value.updated` event fires. The webhook fetches the updated file, locates the existing row by Frame.io File ID, and **updates only the changed cells**. Cells not managed by Frame.io are left untouched.

---

## Environment Variables

> [!IMPORTANT]
> Add these under **Vercel → Project Settings → Environment Variables → Production** before deploying. A missing variable required at startup will crash the app. (Backend credentials are read lazily, so a disabled backend with empty credentials is fine.)

### Frame.io / Adobe

| Variable | Where to get it | Notes |
|---|---|---|
| `FRAMEIO_SIGNING_SECRET` | Frame.io → Settings → Webhooks, shown once at webhook creation | Used to verify every inbound webhook payload |
| `FRAMEIO_ACCOUNT_ID` | Frame.io URL: `next.frame.io/?a=<this value>` | Required to call the Frame.io v4 API |
| `ADOBE_CLIENT_ID` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_CLIENT_SECRET` | Adobe Developer Console → your project | OAuth app credentials |
| `ADOBE_REFRESH_TOKEN` | Captured via the one-time `/oauth/callback` flow (see below) | Long-lived token; rotate if Adobe warns you it changed |
| `OAUTH_CALLBACK_ENABLED` | Set manually | `true` only during the one-time OAuth setup, then set to `false` |

### Google Sheets

| Variable | Where to get it | Notes |
|---|---|---|
| `SHEET_ID` | Spreadsheet URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit` | The target spreadsheet |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → service account → JSON key | Entire JSON file contents as a single value. The service account must be shared (Editor) on the spreadsheet |
| `SHEETS_ENABLED` | Set manually | Defaults to `true`. Set `false` to disable Sheets writes |

The target tab is **routed by Frame.io project name** — the writer matches the asset's project name against the tab titles in the spreadsheet (case-insensitively) and writes there. If no tab matches, the update is skipped and logged. Column names within the tab (the header row) are matched the same way. There are no per-column env vars to configure.

### Airtable (disabled by default)

| Variable | Where to get it | Notes |
|---|---|---|
| `AIRTABLE_ENABLED` | Set manually | Defaults to `false`. Set `true` to re-activate Airtable writes |
| `AIRTABLE_PAT` | Airtable → [Developer hub → Personal access tokens](https://airtable.com/create/tokens) | Needs scopes `schema.bases:read`, `data.records:read`, `data.records:write`, and access to your base |
| `AIRTABLE_BASE_ID` | The base ID from the base URL or [airtable.com/api](https://airtable.com/api) | Starts with `app...` |

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

### 4. Google Sheets Setup

**Create the spreadsheet and tabs:**

1. Create (or open) the Google Sheet that will hold the synced rows. Grab the **spreadsheet ID** from its URL (`docs.google.com/spreadsheets/d/<SHEET_ID>/edit`) and save it as `SHEET_ID`.
2. Create one tab **per Frame.io project**, named to match the project name (case-insensitive — spaces and underscores are ignored). Each tab needs a **header row** (row 1) whose cells match the columns in the [Sheet Structure](#sheet-structure) below; those are matched the same way, so `File ID`, `file_id`, and `fileid` are all equivalent. An asset is written to the tab matching its project name; if none matches, the update is skipped.

**Create a service account:**

3. In the [Google Cloud Console](https://console.cloud.google.com/), enable the **Google Sheets API** for a project.
4. Create a **service account**, then create a **JSON key** for it. Download the JSON file.
5. Save the entire JSON contents as `GOOGLE_SERVICE_ACCOUNT_JSON` in Vercel (single value).
6. **Share the spreadsheet** with the service account's email (found in the JSON as `client_email`) with **Editor** access — otherwise writes return a 403.

### 5. Verify the Integration

Diagnostic endpoints confirm everything is wired up before you rely on live webhooks:

- **`GET /test/accounts`** — lists the Frame.io accounts your OAuth token can see and flags whether your configured `FRAMEIO_ACCOUNT_ID` matches one of them. Use this if the Frame.io API returns errors.
- **`GET /test/sheets`** — verifies the Google credentials and lists every tab in the spreadsheet. Add `?project=<name>` to test routing: it reports which tab that project name resolves to (`resolved_tab`, `matched`) and the resolved `header_map` (internal key → 0-based column index). Any internal key missing from the map means no header matched it.
- **`POST /test/sheets`** with `{"file_id": "...", "project": "<tab name>"}` writes a sample row (omit `project` to use the first tab).

> The Airtable equivalents `GET /test/airtable` and `POST /test/airtable` remain available for diagnostics when Airtable credentials are configured.

---

## Sheet Structure

Each project tab has these columns (header row in row 1), all populated automatically by the webhook:

| Column | Frame.io Source |
|---|---|
| Name | Asset filename |
| File ID | Asset ID |
| SME | `SME` metadata field |
| PM | `PM` metadata field |
| Status | `Overall Video Status` metadata field |
| Notes | `Notes` metadata field |
| Module | `Module` metadata field |
| ID | `ID` metadata field |

The lookup key is **File ID** — every upsert first searches the File ID column for a row matching the Frame.io file ID before deciding whether to insert or update.

Column names are matched **case-insensitively** (spaces and underscores are ignored too), so a header named `Module`, `MODULE`, or `module` all map to the same field. Columns can appear in any order — they are located by header name, not position. Run `GET /test/sheets?project=<name>` after deploying to see the resolved `header_map` for a given project's tab — any internal key without a matching header is logged as a warning and skipped.

---

## Metadata Field Names

> [!IMPORTANT]
> The field names in `METADATA_FIELD_MAP` (`enrichment.py`) are matched against the `field_definition_name` returned by the Frame.io API. Matching is **case-insensitive**, so `Module`, `MODULE`, and `module` all resolve to the same key — but the rest of the name must still match what is configured in your Frame.io account's metadata schema.

```python
# enrichment.py
METADATA_FIELD_MAP = {
    'Overall Video Status': 'status',
    'PM':                   'pm',
    'SME':                  'sme',
    'Notes':                'notes',
    'Production ID':        'production_id',
    'MODULE':               'module',
    'ID':                   'id',
}
```

If a field name drifts in a way casing can't absorb (e.g. renamed from `"PM"` to `"Project Manager"`) the mapping will silently stop syncing that field — update the key here to match.

The internal key (right-hand side) is then matched against your sheet headers by `_INTERNAL_KEYS` in `sheets_writer.py` (and, when enabled, `airtable_writer.py`), also case-insensitively. To sync a brand-new field end to end: add it here, add a matching entry to `_INTERNAL_KEYS`, and make sure a sheet column with that name exists.

---

## Re-enabling Airtable

The Airtable writer (`airtable_writer.py`) is fully intact but off by default. To switch it back on:

1. Set `AIRTABLE_ENABLED=true` and provide `AIRTABLE_PAT` + `AIRTABLE_BASE_ID`.
2. Optionally set `SHEETS_ENABLED=false` if you want Airtable *instead of* Sheets (leave it `true` to write to both).
3. Redeploy.

Airtable routes by table name and matches columns by name in exactly the same way as the Sheets backend. Use `GET /test/airtable` / `GET /test/airtable?project=<name>` to verify credentials and routing.
