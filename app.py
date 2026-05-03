import os
import json
import hmac
import hashlib
import time
import logging
import requests
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

@app.route('/debug', methods=['GET'])
def debug():
    return jsonify({
        'has_signing_secret': bool(os.environ.get('FRAMEIO_SIGNING_SECRET')),
        'has_sheet_id': bool(os.environ.get('SHEET_ID')),
        'has_google_creds': bool(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')),
        'sheet_id_length': len(os.environ.get('SHEET_ID', '')),
    }), 200

@app.route('/test-write', methods=['POST'])
def test_write():
    fake_event = {
        'created_at': '2026-05-02T12:00:00Z',
        'type': 'test.event',
        'resource': {'id': 'test-123', 'name': 'test asset', 'status': 'approved'},
        'user': {'email': 'test@example.com'},
    }
    try:
        write_to_sheet(fake_event)
        return jsonify(success=True), 200
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/oauth/callback', methods=['GET'])
def oauth_callback():
    """One-time use route to capture the authorization code from Adobe IMS."""
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        return f"OAuth error: {error}", 400
    
    if not code:
        return "Missing authorization code", 400
    
    # Exchange the code for tokens
    response = requests.post(
        'https://ims-na1.adobelogin.com/ims/token/v3',
        data={
            'grant_type': 'authorization_code',
            'client_id': os.environ['ADOBE_CLIENT_ID'],
            'client_secret': os.environ['ADOBE_CLIENT_SECRET'],
            'code': code,
        },
        timeout=10,
    )
    
    if response.status_code != 200:
        return f"Token exchange failed: {response.text}", 500
    
    tokens = response.json()
    
    # Display the refresh token so you can copy it into env vars
    # SECURITY: Remove this route after you've captured the token!
    return f"""
    <html>
    <body style="font-family: monospace; padding: 20px;">
    <h2>Save this refresh_token as ADOBE_REFRESH_TOKEN in Vercel:</h2>
    <textarea style="width:100%; height:100px;">{tokens.get('refresh_token', '')}</textarea>
    <h3>Access token (short-lived, just for verification):</h3>
    <textarea style="width:100%; height:100px;">{tokens.get('access_token', '')}</textarea>
    <p>Expires in: {tokens.get('expires_in')} seconds</p>
    <p style="color:red"><strong>DELETE THIS ROUTE after you save the refresh token!</strong></p>
    </body>
    </html>
    """