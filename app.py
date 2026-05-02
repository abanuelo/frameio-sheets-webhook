import os
import json
import hmac
import hashlib
import time
import logging
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNING_SECRET = os.environ['FRAMEIO_SIGNING_SECRET']
SHEET_ID = os.environ['SHEET_ID']
GOOGLE_CREDS = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

app = Flask(__name__)

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
    now = time.time()
    expired = [k for k, v in _recent_events.items() if now - v > DEDUP_WINDOW_SECONDS]
    for k in expired:
        del _recent_events[k]
    if event_id in _recent_events:
        return True
    _recent_events[event_id] = now
    return False


def write_to_sheet(event: dict):
    credentials = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    service = build('sheets', 'v4', credentials=credentials, cache_discovery=False)
    
    row = [
        event.get('created_at', ''),
        event.get('type', ''),
        event.get('resource', {}).get('id', ''),
        event.get('resource', {}).get('name', ''),
        event.get('resource', {}).get('status', ''),
        event.get('user', {}).get('email', ''),
    ]
    
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range='Sheet1!A:F',
        valueInputOption='USER_ENTERED',
        body={'values': [row]}
    ).execute()


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

        if event_id and is_duplicate(event_id):
            return jsonify(received=True, duplicate=True), 200

        write_to_sheet(event)
        return jsonify(received=True), 200

    except HttpError as e:
        logger.error(f"Sheets API error: {e}")
        return 'sheets error', 500
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 'internal error', 500


@app.route('/', methods=['GET'])
@app.route('/health', methods=['GET'])
def health():
    return jsonify(status='ok'), 200