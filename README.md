# Frame.io → Airtable Webhook

Automatically syncs Frame.io asset activity to an Airtable table. When a new file is uploaded it appears as a new row. When metadata fields on an existing asset change, the corresponding row is updated in place.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Environment Variables](#environment-variables)
3. [Setup Guide](#setup-guide)
   - [Deploy to Vercel](#1-deploy-to-vercel)
   - [Configure the Frame.io Webhook](#2-configure-the-frameio-webhook)
   - [Adobe Developer Console (Frame.io API)](#3-adobe-developer-console-frameio-api)
   - [Airtable Setup](#4-airtable-setup)
   - [Verify the Integration](#5-verify-the-integration)
4. [Table Structure](#table-structure)
5. [Metadata Field Names](#metadata-field-names)

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
               └─► upsert_record()   ← airtable_writer.py
                        │
                        ├─ Discovers the table + columns via the Airtable meta API
                        ├─ Searches the table for matching Frame.io File ID
                        ├─ Found ──────────────────────────────► UPDATE row
                        └─ Not found ──────────────────────────► INSERT new row
```

### New Asset Uploaded

When a file is uploaded to Frame.io a `file.created` or `file.ready` event fires. The webhook fetches the full file record and, if no row exists yet for that file ID, **creates a new row** in the Airtable table with all available metadata.

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

### Airtable

| Variable | Where to get it | Notes |
|---|---|---|
| `AIRTABLE_PAT` | Airtable → [Developer hub → Personal access tokens](https://airtable.com/create/tokens) | Needs scopes `schema.bases:read`, `data.records:read`, `data.records:write`, and access to your base |
| `AIRTABLE_BASE_ID` | The base ID from the base URL or [airtable.com/api](https://airtable.com/api) | Starts with `app...` |

The table itself is **auto-discovered** — the writer reads the first table in the base and matches its column names to the internal keys (case-insensitively). There are no per-column ID env vars to configure.

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

### 4. Airtable Setup

**Create the base and table:**

1. Create (or open) the Airtable base that will hold the synced rows.
2. In its first table, create columns whose names match the [Table Structure](#table-structure) below. Names are matched case-insensitively and ignore spaces/underscores, so `File ID`, `file_id`, and `fileid` are all equivalent. The writer uses the **first table** in the base.
3. Grab the **base ID** from the base URL (`airtable.com/app.../...`) or from [airtable.com/api](https://airtable.com/api), and save it as `AIRTABLE_BASE_ID`.

**Create a Personal Access Token:**

4. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens) → **Create new token**.
5. Add these scopes: `schema.bases:read`, `data.records:read`, `data.records:write`.
6. Under **Access**, add the base from step 1.
7. Copy the token and save it as `AIRTABLE_PAT` in Vercel.

> [!NOTE]
> The token must have **both** the schema scope (so the app can discover the table and its columns) and the record scopes (so it can read/write rows). Missing the schema scope is the most common cause of `Airtable API error 403` on the first request.

### 5. Verify the Integration

Two diagnostic endpoints confirm everything is wired up before you rely on live webhooks:

- **`GET /test/accounts`** — lists the Frame.io accounts your OAuth token can see and flags whether your configured `FRAMEIO_ACCOUNT_ID` matches one of them. Use this if the Frame.io API returns errors.
- **`GET /test/airtable`** — verifies the Airtable credentials, shows the discovered table name, and prints the resolved `field_map` (internal key → actual column). Any internal key missing from the map means no Airtable column matched it.

You can also `POST /test/airtable` with `{"file_id": "..."}` to write a sample row end to end.

---

## Table Structure

The Airtable table has 8 columns, all populated automatically by the webhook:

| Column | Type | Frame.io Source | Select Options |
|---|---|---|---|
| Name | text | Asset filename | — |
| File ID | text | Asset ID | — |
| SME | select | `SME` metadata field | Needs Review, In Progress, Approved, N/A |
| PM | select | `PM` metadata field | Needs Review, In Progress, Approved, N/A |
| Status | select | `Overall Video Status` metadata field | Rough Cut Ready, R1 Comments, R2 Comments, R2 Edits, Approvals, Full Length Lecture |
| Notes | text | `Notes` metadata field | — |
| Module | text | `Module` metadata field | — |
| ID | text | `ID` metadata field | — |

The lookup key is **File ID** — every upsert first searches the table for a row with a matching Frame.io file ID before deciding whether to create or update.

Column names are matched **case-insensitively** (spaces and underscores are ignored too), so an Airtable column named `Module`, `MODULE`, or `module` all map to the same field. Run `GET /test/airtable` after deploying to see the resolved `field_map` — any internal key without a matching Airtable column is logged as a warning and skipped.

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

The internal key (right-hand side) is then matched against your Airtable columns by `_INTERNAL_KEYS` in `airtable_writer.py`, also case-insensitively. To sync a brand-new field end to end: add it here, add a matching entry to `_INTERNAL_KEYS`, and make sure an Airtable column with that name exists.
