"""Webhook event handlers: fetch full file data, parse, write to Airtable."""
import os
import logging
from frameio_client import get_file, parse_metadata
from airtable_writer import upsert_record

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ['FRAMEIO_ACCOUNT_ID']

# Frame.io metadata field name → internal key used by airtable_writer
METADATA_FIELD_MAP = {
    'Overall Video Status': 'status',
    'PM': 'pm',
    'SME': 'sme',
    'Notes': 'notes',
    'Production ID': 'production_id',
}

# Events that warrant fetching full file + updating Airtable
ENRICHMENT_EVENTS = {
    'file.created',
    'file.ready',
    'file.label.updated',
    'metadata.value.updated',
}


def _extract_file_id(event: dict) -> str:
    """Get the file ID from various possible locations in the webhook payload."""
    resource = event.get('resource') or {}
    return resource.get('id') or event.get('file_id') or ''


def handle_event(event: dict):
    """Main entry point. Return True if something was written to Airtable."""
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

    # Filename goes into the Name (Production ID) column
    filename = file_data.get('name', '')

    updates = {
        'frameio_file_id': file_id,
        'production_id': filename,
    }

    # Map Frame.io metadata fields to Airtable columns
    for fio_field_name, key in METADATA_FIELD_MAP.items():
        if fio_field_name in metadata:
            value = metadata[fio_field_name]
            if isinstance(value, list):
                continue
            updates[key] = value

    try:
        result = upsert_record(updates)
        logger.info(f"Airtable update result: {result} for file {file_id}")
        return True
    except Exception as e:
        logger.exception(f"Failed to update Airtable for file {file_id}: {e}")
        return False
