# Frame.io → Google Sheets Webhook

Automatically syncs Frame.io asset activity to a Google Sheet. When a new file is uploaded it appears as a new row. When metadata fields on an existing asset change, the corresponding row is updated in place.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Configuration (`config.json`)](#configuration-configjson)
3. [Environment Variables](#environment-variables)
4. [Setup Guide](#setup-guide)
   - [Deploy to Vercel](#1-deploy-to-vercel)
   - [Configure the Frame.io Webhook](#2-configure-the-frameio-webhook)
   - [Adobe Developer Console (Frame.io API)](#3-adobe-developer-console-frameio-api)
   - [Google Sheets Setup](#4-google-sheets-setup)
   - [Verify the Integration](#5-verify-the-integration)
5. [Sheet Structure](#sheet-structure)

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
               │   (file.created, file.ready, file.label.updated, file.versioned, metadata.value.updated)
               │
               ├─ Fetches full file data from Frame.io API (includes metadata fields)
               │
               ├─ Maps Frame.io metadata field names → sheet columns (config.json)
               │
               └─► upsert_record()   ← sheets_writer.py
                        │
                        ├─ Picks the tab whose name matches the asset's Frame.io project (case-insensitive)
                        ├─ Searches the tab for a matching Frame.io File ID
                        ├─ Found ──────────────────────────────► UPDATE row
                        └─ Not found ──────────────────────────► INSERT new row
```

Writes can be turned off entirely with `SHEETS_ENABLED=false` (e.g. for a dry run).

### New Asset Uploaded

When a file is uploaded to Frame.io a `file.created` or `file.ready` event fires. The webhook fetches the full file record and, if no row exists yet for that file ID, **creates a new row** in the matching tab with all available metadata.

### Metadata Field Changed

When someone edits a custom metadata field on an existing asset a `metadata.value.updated` event fires. The webhook fetches the updated file, locates the existing row by Frame.io File ID, and **updates only the changed cells**. Cells not managed by Frame.io are left untouched.

### Version Stacked (e.g. R1 → R2)

When an edit is version-stacked, Frame.io creates a **new file asset** (new File ID) and fires `file.versioned`. The webhook detects that the asset belongs to a version stack, looks up the stack's other versions, and finds the **existing row** keyed by any prior version's File ID. It then **updates that same row in place** — swapping in the new File ID and the new status — instead of inserting a duplicate row.

### Removed From Tracking (terminal status)

When an asset's status becomes a terminal value (by default `Full Length Lecture`, set by `removal_statuses` in `config.json`), the asset has left this project's tracking, so the webhook **deletes its row** rather than updating it. Two guards apply:

- The new status must be in `removal_statuses` — any other status just updates the row.
- The row's **previous** status (the value currently in its Status cell) must be one of `deletable_prior_statuses` (default `R1 Edits` / `R2 Edits`). A blank or any other prior status keeps the row and simply writes the new status.

If the delete is skipped by these guards, the new status is still written. If no matching row exists, the delete is a no-op.

---

## Configuration (`config.json`)

Everything about *what* syncs — which Frame.io fields go to which sheet columns, and the deletion rules — lives in one file: **`config.json`**. You do not need to touch any Python. Edit it, commit, and redeploy.

If `config.json` is missing or has a typo, the app falls back to the built-in defaults and logs a message — it won't crash.

```json
{
  "field_mappings": {
    "Status": "Status",
    "PM": "PM",
    "SME": "SME",
    "Notes": "Notes",
    "MODULE": "Module",
    "ID": "ID"
  },

  "file_id_column": "File ID",
  "filename_column": "Name",
  "status_column": "Status",

  "removal_statuses": ["Full Length Lecture"],
  "deletable_prior_statuses": ["R1 Edits", "R2 Edits"]
}
```

### `field_mappings` — the important one

This is where you add fields. Each line is:

```
"<Frame.io field name>": "<Google Sheet column header>"
```

- **Left** = the field name exactly as it appears in Frame.io (the metadata field's name).
- **Right** = the column header in your Google Sheet where that value should be written.

Matching ignores case and spaces on both sides, so `"MODULE": "Module"` works even if the sheet header is `module`.

**To add a new field to sync:** add one line, make sure a column with that header exists in your sheet tab, and redeploy. For example, to start syncing a Frame.io field called `Editor` into a `Editor` column:

```json
  "field_mappings": {
    "Status": "Status",
    "Editor": "Editor",     ← added
    ...
  }
```

**To stop syncing a field:** delete its line. **To send a field to a differently-named column:** change the right-hand side, e.g. `"Notes": "Producer Notes"`.

### The special columns

| Key | What it is |
|---|---|
| `file_id_column` | The sheet column that stores the Frame.io **File ID**. This is how a row is found and updated (and how version stacks collapse). **Required** — the matching column must exist in every tab. |
| `filename_column` | The sheet column that gets the asset's filename. Set to `""` to skip writing it. |
| `status_column` | Which sheet column holds the status. Drives the deletion rules. Should be the same column you mapped `Status` to. |

### The deletion rules

| Key | What it does |
|---|---|
| `removal_statuses` | When an asset's status changes **to** one of these, its row may be deleted instead of updated. Set to `[]` to never auto-delete. |
| `deletable_prior_statuses` | The delete only happens if the row's **current** status is one of these first. Any other prior status (or blank) keeps the row and just updates the status. |

> [!TIP]
> Field names must match Frame.io exactly (aside from case/spaces). If a field silently stops syncing, the most likely cause is a rename in Frame.io — check the webhook logs, which print `writing columns [...]` for every event, then update the left-hand side in `field_mappings`.

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

The target tab is **routed by Frame.io project name** — the writer matches the asset's project name against the tab titles in the spreadsheet (case-insensitively) and writes there. If no tab matches, the update is skipped and logged. Which fields land in which columns is controlled by [`config.json`](#configuration-configjson), not env vars.

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

**Create the spreadsheet and tabs:**

1. Create (or open) the Google Sheet that will hold the synced rows. Grab the **spreadsheet ID** from its URL — it's the long string between `/d/` and `/edit`:
   ```
   docs.google.com/spreadsheets/d/<SHEET_ID>/edit
   ```
   Save it as `SHEET_ID`.
2. Create one tab **per Frame.io project**, named to match the project name (case-insensitive — spaces and underscores are ignored). Each tab needs a **header row** (row 1) whose cells match the column names in your [`config.json`](#configuration-configjson); those are matched the same way, so `File ID`, `file_id`, and `fileid` are all equivalent. An asset is written to the tab matching its project name; if none matches, the update is skipped.

**What a service account is:** a non-human Google identity your app authenticates as. You create one in Google Cloud, download its key as a JSON file, and the *contents* of that file become the `GOOGLE_SERVICE_ACCOUNT_JSON` env var.

**Create the service account:**

3. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project (or pick an existing one) using the project dropdown at the top.
4. Enable the Sheets API: go to [APIs & Services → Library → Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com) and click **Enable**.
5. Go to [APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) → **+ Create Credentials → Service account**. Give it a name (e.g. `frameio-sheets-writer`) → **Create and continue**. The optional role/access steps can be skipped → **Done**.

**Create a JSON key:**

6. In **Credentials**, click the service account you just created.
7. Open the **Keys** tab → **Add Key → Create new key** → choose **JSON** → **Create**. The `.json` file downloads automatically.

> [!IMPORTANT]
> The JSON key downloads **once** — Google won't let you re-download it. If you lose it, create a new key (and delete the old one). Treat it like a password; never commit it to git.

**Set the env var:**

8. `GOOGLE_SERVICE_ACCOUNT_JSON` takes the **entire contents** of that JSON file (it's parsed with `json.loads()` in `sheets_writer.py`).
   - **In Vercel:** paste the whole JSON blob as the value. Multi-line values are fine.
   - **In a local `.env`:** put it on a single line. Flatten it with:
     ```bash
     cat service-account.json | jq -c .
     ```
     then paste that one-line output as the value.

**Share the spreadsheet:** ⚠️ easy to forget

9. Open the JSON file and copy the `client_email` value (looks like `frameio-sheets-writer@your-project.iam.gserviceaccount.com`). In your Google Sheet, click **Share** and add that email with **Editor** access.

> [!IMPORTANT]
> Without sharing the sheet with the service account, every write returns a **403**. This is the most common setup mistake.

### 5. Verify the Integration

1. Hit **`GET /health`** — it should return `{"status": "ok"}`, confirming the app is deployed and running.
2. In Frame.io, change a metadata field (e.g. `Status`) on an asset in a project that has a matching sheet tab.
3. Watch the Vercel logs. For each event the app logs `File <id> (<event>): writing columns [...]` followed by `Sheets update result: updated/inserted ...`. If a field you expect is missing from `writing columns`, its Frame.io name doesn't match the left-hand side in `config.json`.
4. Confirm the row appears/updates in the sheet.

Common issues surface clearly in the logs: `No sheet tab matches project ...` (tab name mismatch), `no column matches ...` (a `config.json` column isn't in the header row), or a `403` (the sheet isn't shared with the service account).

---

## Sheet Structure

Each project tab has a header row (row 1). The default columns, all populated automatically by the webhook, are:

| Column | Frame.io Source |
|---|---|
| Name | Asset filename (`filename_column`) |
| File ID | Asset ID (`file_id_column`) |
| SME | `SME` metadata field |
| PM | `PM` metadata field |
| Status | `Status` metadata field |
| Notes | `Notes` metadata field |
| Module | `MODULE` metadata field |
| ID | `ID` metadata field |

These are just the defaults — add, remove, or rename columns by editing [`config.json`](#configuration-configjson). The `Name` and `File ID` columns come from `filename_column` / `file_id_column`; the rest come from `field_mappings`.

The lookup key is **File ID** — every upsert first searches the File ID column for a row matching the Frame.io file ID before deciding whether to insert or update.

Column names are matched **case-insensitively** (spaces and underscores are ignored too), so a header named `Module`, `MODULE`, or `module` all map to the same field. Columns can appear in any order — they are located by header name, not position. Any configured column without a matching header is logged as a warning and skipped.
