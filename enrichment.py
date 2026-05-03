"""Webhook event handlers: fetch full file data, parse, write to sheet."""
import os
import re
import logging
from frameio_client import get_file, parse_metadata, get_project
from sheets_writer import upsert_project_row

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ['FRAMEIO_ACCOUNT_ID']

# Frame.io metadata field name → our internal sheet key
# Only fields that should sync from Frame.io. Speaker, Release, Editor
# are managed manually in the sheet.
METADATA_FIELD_TO_SHEET_KEY = {
    'Overall Video Status': 'status',
    'PM': 'pm',
    'SME': 'sme',
    'Notes': 'notes',
    'Production ID': 'production_id',
}

# Events that warrant fetching full file + updating project tab
ENRICHMENT_EVENTS = {
    'file.created',
    'file.ready',
    'file.label.updated',
    'metadata.value.updated',
}


def _project_tab_name(file_data: dict) -> str:
    """The project tab in the sheet matches the Frame.io project name."""
    project = file_data.get('project') or {}
    return project.get('name', '').strip()


def _extract_production_id_from_filename(filename: str) -> str:
    """Fallback: pull AICS229-2026-L01-S01 style ID from the filename."""
    if not filename:
        return ''
    match = re.search(r'AICS\d+-\d+-L\d+-S\d+', filename, re.IGNORECASE)
    return match.group(0).upper() if match else ''


def _extract_file_id(event: dict) -> str:
    """Get the file ID from various possible locations in the webhook payload."""
    resource = event.get('resource') or {}
    return resource.get('id') or event.get('file_id') or ''


def handle_event(event: dict):
    """Main entry point. Return True if something was written to project tab."""
    event_type = event.get('type', '')
    
    if event_type not in ENRICHMENT_EVENTS:
        logger.info(f"Skipping enrichment for event type: {event_type}")
        return False
    
    file_id = _extract_file_id(event)
    if not file_id:
        logger.warning(f"No file_id in event {event.get('id')}, skipping enrichment")
        return False
    
    try:
        file_data = get_file(ACCOUNT_ID, file_id)
    except Exception as e:
        logger.exception(f"Failed to fetch file {file_id}: {e}")
        return False
    
    metadata = parse_metadata(file_data)
    
    # Build the sheet update payload — only fields we sync from Frame.io
    updates = {
        'frameio_file_id': file_id,
        'name': file_data.get('name', ''),
    }
    
    for fio_field_name, sheet_key in METADATA_FIELD_TO_SHEET_KEY.items():
        if fio_field_name in metadata:
            value = metadata[fio_field_name]
            if isinstance(value, list):
                continue
            updates[sheet_key] = value
    
    # Production ID fallback: try filename if not set as metadata
    if not updates.get('production_id'):
        updates['production_id'] = _extract_production_id_from_filename(updates['name'])
    
    project_tab = _project_tab_name(file_data)
    if not project_tab:
        logger.warning(f"No project name for file {file_id}, skipping")
        return False
    
    try:
        result = upsert_project_row(project_tab, updates)
        logger.info(f"Sheet update result: {result} for file {file_id} in tab '{project_tab}'")
        return True
    except Exception as e:
        logger.exception(f"Failed to update sheet for file {file_id}: {e}")
        return False
    
def _project_tab_name(file_data: dict) -> str:
    """Get project name. Falls back to fetching project by ID if not embedded."""
    project = file_data.get('project') or {}
    if project.get('name'):
        return project['name'].strip()
    
    project_id = file_data.get('project_id')
    if not project_id:
        return ''
    
    try:
        project_data = get_project(ACCOUNT_ID, project_id)
        return project_data.get('name', '').strip()
    except Exception as e:
        logger.warning(f"Couldn't fetch project {project_id}: {e}")
        return ''