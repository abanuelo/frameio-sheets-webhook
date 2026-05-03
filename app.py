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
        event_id = event.get('id', '')
        
        if is_duplicate(event_id):
            return jsonify(received=True, duplicate=True), 200

        # 1. Always log to events tab
        try:
            append_event_row(event)
        except Exception as e:
            logger.exception(f"Failed to append event row: {e}")

        # 2. Enrich and update project tab if applicable
        try:
            handle_event(event)
        except Exception as e:
            # Don't fail the webhook if enrichment fails
            logger.exception(f"Enrichment failed: {e}")

        return jsonify(received=True), 200

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        return 'bad request', 200  # Don't retry on bad JSON

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 'internal error', 500


@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    return jsonify(status='ok'), 200