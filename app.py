import os
import json
import hmac
import hashlib
import time
import logging
from flask import Flask, request, jsonify

from sheets_writer import append_event_row
from enrichment import handle_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SIGNING_SECRET = os.environ['FRAMEIO_SIGNING_SECRET']

app = Flask(__name__)


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

        # 1. Always log to events tab
        try:
            append_event_row(event)
        except Exception as e:
            logger.exception(f"Failed to append event row: {e}")

        # 2. Enrich and update project tab if applicable
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

@app.route('/oauth/callback', methods=['GET'])
def oauth_callback():
    """
    One-time OAuth callback for capturing a fresh refresh token.
    
    Disabled by default. To re-enable for token refresh:
    1. Set OAUTH_CALLBACK_ENABLED=true in Vercel env vars
    2. Redeploy
    3. Visit the consent URL in your browser
    4. Capture the refresh token
    5. Update ADOBE_REFRESH_TOKEN env var
    6. Set OAUTH_CALLBACK_ENABLED=false (or remove it)
    7. Redeploy
    """
    if os.environ.get('OAUTH_CALLBACK_ENABLED', '').lower() != 'true':
        return 'OAuth callback disabled. Set OAUTH_CALLBACK_ENABLED=true to enable.', 403
    
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        return f"OAuth error: {error}", 400
    if not code:
        return "Missing authorization code", 400
    
    try:
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
    except Exception as e:
        return f"Network error: {e}", 500
    
    if response.status_code != 200:
        return f"Token exchange failed: {response.text}", 500
    
    tokens = response.json()
    
    return f"""
    <html>
    <body style="font-family: monospace; padding: 20px; background: #f5f5f5;">
    <h2>OAuth Token Captured</h2>
    
    <h3>Refresh Token (save as ADOBE_REFRESH_TOKEN in Vercel):</h3>
    <textarea style="width:100%; height:120px; font-family: monospace;">{tokens.get('refresh_token', '')}</textarea>
    
    <h3>Granted Scopes:</h3>
    <pre>{tokens.get('scope', 'NOT RETURNED')}</pre>
    
    <h3>Access Token (short-lived, just for verification):</h3>
    <textarea style="width:100%; height:80px; font-family: monospace;">{tokens.get('access_token', '')}</textarea>
    
    <p>Expires in: {tokens.get('expires_in')} seconds</p>
    
    <hr>
    <p style="color:red; font-weight:bold;">
        ⚠ Once you've saved the refresh token, set OAUTH_CALLBACK_ENABLED=false 
        (or remove it) in Vercel env vars and redeploy. Leaving this route open 
        is a security risk.
    </p>
    </body>
    </html>
    """

@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    return jsonify(status='ok'), 200