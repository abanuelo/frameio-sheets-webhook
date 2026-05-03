"""Two write paths: append-only event log + upsert by Production ID into project tab."""
import os
import json
import logging
from datetime import datetime, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SHEET_ID = os.environ['SHEET_ID']
GOOGLE_CREDS = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

EVENTS_TAB = 'webhook events'

# Test tab columns (matches your sheet layout)
COL_STATUS = 'A'
COL_FRAMEIO_FILE_ID = 'B'
COL_PRODUCTION_ID = 'C'
COL_NAME = 'D'
COL_RELEASE = 'E'
COL_SPEAKER = 'F'
COL_SME = 'G'
COL_PM = 'H'
COL_NOTES = 'I'
COL_EDITOR = 'J'

# Field mapping: sheet column key → spreadsheet column letter
SHEET_COLUMNS = {
    'status': COL_STATUS,
    'frameio_file_id': COL_FRAMEIO_FILE_ID,
    'production_id': COL_PRODUCTION_ID,
    'name': COL_NAME,
    'release': COL_RELEASE,
    'speaker': COL_SPEAKER,
    'sme': COL_SME,
    'pm': COL_PM,
    'notes': COL_NOTES,
    'editor': COL_EDITOR,
}

# Column order for inserts (must match A through J)
COLUMN_ORDER = ['status', 'frameio_file_id', 'production_id', 'name',
                'release', 'speaker', 'sme', 'pm', 'notes', 'editor']


def _service():
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def append_event_row(event: dict):
    """Append every webhook to the events tab."""
    svc = _service()
    timestamp = datetime.now(timezone.utc).isoformat()
    
    row = [
        event.get('type', ''),
        timestamp,
        json.dumps(event)[:50000],  # cell limit safety
    ]
    
    svc.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{EVENTS_TAB}'!A:C",
        valueInputOption='USER_ENTERED',
        body={'values': [row]}
    ).execute()
    logger.info(f"Appended event {event.get('type')} to {EVENTS_TAB}")


def find_row_by_production_id(svc, project_tab: str, production_id: str):
    """Return 1-indexed row number, or None if not found."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{project_tab}'!{COL_PRODUCTION_ID}:{COL_PRODUCTION_ID}",
    ).execute()
    
    values = result.get('values', [])
    for i, r in enumerate(values):
        if r and r[0].strip() == production_id.strip():
            return i + 1
    return None


def find_row_by_file_id(svc, project_tab: str, file_id: str):
    """Fallback lookup: find row by Frame.io File ID."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{project_tab}'!{COL_FRAMEIO_FILE_ID}:{COL_FRAMEIO_FILE_ID}",
    ).execute()
    
    values = result.get('values', [])
    for i, r in enumerate(values):
        if r and r[0].strip() == file_id.strip():
            return i + 1
    return None


def upsert_project_row(project_tab: str, updates: dict):
    """
    Update or insert a row in the project tab.
    
    Match priority:
    1. By production_id if present in updates
    2. By frameio_file_id if production_id missing or no match
    3. Insert new row if neither matches
    
    `updates` keys must be from SHEET_COLUMNS. Empty/None values are skipped 
    (won't overwrite existing cells with blanks).
    """
    svc = _service()
    
    # Find existing row
    row_index = None
    if updates.get('production_id'):
        row_index = find_row_by_production_id(svc, project_tab, updates['production_id'])
    
    if row_index is None and updates.get('frameio_file_id'):
        row_index = find_row_by_file_id(svc, project_tab, updates['frameio_file_id'])
    
    if row_index is None:
        # Insert new row at end
        new_row = [updates.get(key, '') for key in COLUMN_ORDER]
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"'{project_tab}'!A:J",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': [new_row]}
        ).execute()
        logger.info(f"Inserted new row in '{project_tab}' for {updates.get('production_id') or updates.get('frameio_file_id')}")
        return 'inserted'
    
    # Update only the cells that have values - don't overwrite with blanks
    data = []
    for key, value in updates.items():
        if value in (None, ''):
            continue
        col = SHEET_COLUMNS.get(key)
        if not col:
            continue
        data.append({
            'range': f"'{project_tab}'!{col}{row_index}",
            'values': [[value]],
        })
    
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={'valueInputOption': 'USER_ENTERED', 'data': data}
        ).execute()
        logger.info(f"Updated row {row_index} in '{project_tab}' with {len(data)} fields")
    
    return 'updated'