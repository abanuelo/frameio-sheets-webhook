import os
import json
import hmac
import hashlib
import logging
from http.server import BaseHTTPRequestHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load config once at module level (reused across warm invocations)
SIGNING_SECRET = os.environ['FRAMEIO_SIGNING_SECRET']
SHEET_ID = os.environ['SHEET_ID']
GOOGLE_CREDS = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

# In-memory dedup cache (resets on cold start, which is fine for short windows)
# For stronger guarantees, use Upstash Redis or similar
_recent_events = {}
DEDUP_WINDOW_SECONDS = 300


def verify_signature(raw_body: bytes, signature: str) -> bool:
    """Constant-time HMAC verification."""
    if not signature:
        return False
    expected = hmac.new(
        SIGNING_SECRET.encode(),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def is_duplicate(event_id: str) -> bool:
    """Simple in-memory dedup. Cold starts will lose state - that's acceptable."""
    import time
    now = time.time()
    # Clean old entries
    expired = [k for k, v in _recent_events.items() if now - v > DEDUP_WINDOW_SECONDS]
    for k in expired:
        del _recent_events[k]
    
    if event_id in _recent_events:
        return True
    _recent_events[event_id] = now
    return False


def get_sheets_service():
    """Build authenticated Sheets client."""
    credentials = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=credentials, cache_discovery=False)


def extract_row(event: dict) -> list:
    """Transform Frame.io event into a sheet row.
    
    NOTE: Adjust field paths to match actual Frame.io v4 payload structure.
    Inspect real payloads first via webhook.site or your logs.
    """
    return [
        event.get('created_at', ''),
        event.get('type', ''),
        event.get('resource', {}).get('id', ''),
        event.get('resource', {}).get('name', ''),
        event.get('resource', {}).get('status', ''),
        event.get('project', {}).get('name', ''),
        event.get('user', {}).get('email', ''),
    ]


def write_to_sheet(event: dict):
    """Append a row to the configured sheet."""
    service = get_sheets_service()
    row = extract_row(event)
    
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range='Sheet1!A:G',
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body={'values': [row]}
    ).execute()
    
    logger.info(f"Wrote row for event {event.get('id', 'unknown')}")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            raw_body = self.rfile.read(content_length)
            signature = self.headers.get('X-Frameio-Signature', '')

            # 1. Verify HMAC - reject invalid requests
            if not verify_signature(raw_body, signature):
                logger.warning("Invalid signature")
                self.send_response(401)
                self.end_headers()
                return

            # 2. Parse payload
            event = json.loads(raw_body)
            event_id = event.get('id', '')

            # 3. Idempotency check
            if event_id and is_duplicate(event_id):
                logger.info(f"Duplicate event {event_id}, skipping")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"received": true, "duplicate": true}')
                return

            # 4. Process
            write_to_sheet(event)

            # 5. Acknowledge
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"received": true}')

        except HttpError as e:
            # Sheets API errors - return 500 so Frame.io retries
            logger.error(f"Sheets API error: {e}")
            self.send_response(500)
            self.end_headers()

        except json.JSONDecodeError as e:
            # Bad payload - return 200 to prevent retries on malformed data
            logger.error(f"Invalid JSON: {e}")
            self.send_response(200)
            self.end_headers()

        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')