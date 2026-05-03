import os
import json
import hmac
import hashlib
import time
import logging
import requests
from flask import Flask, request, jsonify

from sheets_writer import append_event_row
from enrichment import handle_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNING_SECRET = os.environ['FRAMEIO_SIGNING_SECRET']

app = Flask(__name__)

# Simple in-memory dedup
_recent_events = {}
DEDUP_WINDOW_SECONDS = 300


def verify_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    if not signature or not timestamp:
        return False
    try:
        req_time = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - req_time) > 300:
        return False
    message = f'v0:{timestamp}:{raw_body.decode("utf-8")}'
    expected = 'v0=' + hmac.new(
        SIGNING_SECRET.encode('latin-1'),
        message.encode('latin-1'),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def is_duplicate(event_id: str) -> bool:
    if not event_id:
        return False
    now = time.time()
    expired = [k for k, v in _recent_events.items() if now - v > DEDUP_WINDOW_SECONDS]
    for k in expired:
        del _recent_events[k]
    if event_id in _recent_events:
        return True
    _recent_events[event_id] = now
    return False

@app.route('/api/webhook', methods=['POST'])
def webhook():
    raw_body = request.get_data()
    signature = request.headers.get('X-Frameio-Signature', '')
    timestamp = request.headers.get('X-Frameio-Request-Timestamp', '')

    if not verify_signature(raw_body, signature, timestamp):
        logger.warning("Invalid signature")
        return 'invalid signature', 401

    try:
        event = json.loads(raw_body)
        
        # TEMP: log full payload for debugging
        logger.info(f"=== FULL WEBHOOK PAYLOAD ===")
        logger.info(json.dumps(event, indent=2))
        logger.info(f"=== END PAYLOAD ===")
        
        event_id = event.get('id', '')
        
        if is_duplicate(event_id):
            return jsonify(received=True, duplicate=True), 200

        try:
            append_event_row(event)
        except Exception as e:
            logger.exception(f"Failed to append event row: {e}")

        try:
            handle_event(event)
        except Exception as e:
            logger.exception(f"Enrichment failed: {e}")

        return jsonify(received=True), 200

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        return 'bad request', 200

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 'internal error', 500

@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    return jsonify(status='ok'), 200

@app.route('/test-enrich/<file_id>', methods=['GET'])
def test_enrich(file_id):
    """Manually trigger enrichment for a file and return what happened."""
    from frameio_client import get_file, parse_metadata
    from enrichment import handle_event, METADATA_FIELD_TO_SHEET_KEY, _extract_production_id_from_filename
    from sheets_writer import upsert_project_row
    
    account_id = os.environ['FRAMEIO_ACCOUNT_ID']
    
    debug = {}
    
    try:
        file_data = get_file(account_id, file_id)
        debug['file_name'] = file_data.get('name')
        debug['project_id'] = file_data.get('project_id')
        debug['project_name_from_object'] = (file_data.get('project') or {}).get('name')
        
        metadata = parse_metadata(file_data)
        debug['parsed_metadata_keys'] = list(metadata.keys())
        debug['parsed_metadata'] = metadata
        
        # Build updates the same way handle_event does
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
        
        if not updates.get('production_id'):
            updates['production_id'] = _extract_production_id_from_filename(updates['name'])
        
        debug['updates_to_write'] = updates
        debug['target_tab'] = (file_data.get('project') or {}).get('name', '').strip()
        
        # Try the actual write
        try:
            result = upsert_project_row(debug['target_tab'] or 'Test', updates)
            debug['write_result'] = result
        except Exception as e:
            debug['write_error'] = str(e)
        
        return jsonify(debug), 200
        
    except Exception as e:
        debug['error'] = str(e)
        return jsonify(debug), 500