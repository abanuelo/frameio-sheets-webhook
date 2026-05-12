import os
import csv
import io
import json
import hmac
import hashlib
import time
import logging
import requests
from flask import Flask, request, jsonify, Response, render_template_string

# Sheets event log disabled — replaced by Slack Lists integration
# from sheets_writer import append_event_row
from enrichment import handle_event
from slack_writer import upsert_list_item

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

        # Enrich and update Slack list if applicable
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

@app.route('/oauth/start', methods=['GET'])
def oauth_start():
    """Redirects to Adobe consent screen. Requires OAUTH_CALLBACK_ENABLED=true."""
    if os.environ.get('OAUTH_CALLBACK_ENABLED', '').lower() != 'true':
        return 'OAuth flow disabled. Set OAUTH_CALLBACK_ENABLED=true to enable.', 403

    from urllib.parse import urlencode
    from flask import redirect

    client_id = os.environ.get('ADOBE_CLIENT_ID', '')
    if not client_id:
        return 'ADOBE_CLIENT_ID not configured', 500

    # Force https — Vercel proxies requests as http internally but the public URL is https
    base = request.url_root.rstrip('/')
    if base.startswith('http://') and 'localhost' not in base:
        base = 'https://' + base[len('http://'):]
    callback_url = base + '/oauth/callback'
    params = urlencode({
        'client_id': client_id,
        'scope': 'openid,offline_access,email,profile,additional_info.roles',
        'response_type': 'code',
        'redirect_uri': callback_url,
    })
    return redirect(f"https://ims-na1.adobelogin.com/ims/authorize/v2?{params}")


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

@app.route('/test/slack', methods=['GET'])
def test_slack_config():
    """GET /test/slack — show config and verify the bot can see the list via files.info."""
    import requests as req
    import slack_writer as sw

    config = dict(
        list_id=sw.LIST_ID or None,
        token_set=bool(sw.TOKEN),
        col_name=sw.COL_NAME or None,
        col_file_id=sw.COL_FILE_ID or None,
        col_sme=sw.COL_SME or None,
        col_pm=sw.COL_PM or None,
        col_status=sw.COL_STATUS or None,
        col_notes=sw.COL_NOTES or None,
    )

    # Check token identity and granted scopes via auth.test
    try:
        r = req.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {sw.TOKEN}"},
            timeout=10,
        )
        auth = r.json()
        config["auth_ok"] = auth.get("ok")
        config["auth_error"] = auth.get("error")
        config["bot_user"] = auth.get("user")
        config["workspace"] = auth.get("team")
        # Slack returns granted scopes in the X-OAuth-Scopes response header
        config["granted_scopes"] = r.headers.get("X-OAuth-Scopes", "header_not_returned")
    except Exception as e:
        config["auth_exception"] = str(e)

    return jsonify(config), 200


@app.route('/test/slack', methods=['POST'])
def test_slack_write():
    body = request.get_json(silent=True) or {}
    file_id = body.get("file_id", "test-file-001")

    sample = {
        "frameio_file_id": file_id,
        "production_id":   "TEST — Slack Integration Check",
        "sme":             "Needs Review",
        "pm":              "Needs Review",
        "status":          "Rough Cut Ready",
        "notes":           "Created by /test/slack endpoint",
    }

    try:
        result = upsert_list_item(sample)
        return jsonify(ok=True, action=result, payload=sample), 200
    except Exception as e:
        logger.exception(f"Slack test write failed: {e}")
        return jsonify(ok=False, error=str(e)), 500


_COMMENTS_UI = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Frame.io Comment Export</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #f5f5f7; margin: 0; padding: 40px 20px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.1);
            max-width: 560px; margin: 0 auto; padding: 32px; }
    h1 { font-size: 1.25rem; font-weight: 700; margin: 0 0 6px; }
    p.subtitle { color: #666; font-size: 0.875rem; margin: 0 0 28px; }
    label { display: block; font-size: 0.8rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: .04em; color: #444; margin-bottom: 6px; }
    input[type="text"] { width: 100%; padding: 9px 12px; font-size: 0.95rem;
                         border: 1px solid #d1d5db; border-radius: 6px;
                         outline: none; margin-bottom: 6px; }
    input[type="text"]:focus { border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.15); }
    .hint { font-size: 0.78rem; color: #888; margin: 0 0 20px; }
    .hint code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }
    .presets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
    .preset-btn { font-size: 0.8rem; padding: 5px 10px; border: 1px solid #d1d5db;
                  border-radius: 5px; background: #f9fafb; cursor: pointer;
                  color: #374151; text-decoration: none; }
    .preset-btn:hover { background: #e5e7eb; }
    button[type="submit"] { background: #2563eb; color: #fff; border: none;
                            padding: 10px 22px; font-size: 0.95rem; font-weight: 600;
                            border-radius: 6px; cursor: pointer; width: 100%; }
    button[type="submit"]:hover { background: #1d4ed8; }
  </style>
  <script>
    function setFolder(id) {
      document.getElementById('folder_id').value = id;
    }
  </script>
</head>
<body>
  <div class="card">
    <h1>Frame.io Comment Export</h1>
    <p class="subtitle">Download all comments from a folder as a CSV file.</p>

    <form action="/comments/export" method="get">
      <label for="folder_id">Folder ID</label>
      <input type="text" id="folder_id" name="folder_id"
             value="{{ folder_id }}"
             placeholder="ab89661f-0b80-44ea-93f1-11968b96ac3d" />
      <p class="hint">
        Copy from the Frame.io URL:<br>
        <code>next.frame.io/project/{project_id}/<strong>{folder_id}</strong></code>
      </p>

      {% if presets %}
      <label>Quick select</label>
      <div class="presets">
        {% for name, fid in presets %}
        <a class="preset-btn" href="#" onclick="setFolder('{{ fid }}'); return false;">{{ name }}</a>
        {% endfor %}
      </div>
      {% endif %}

      <div style="display:flex;gap:10px">
        <button type="submit" formaction="/comments/scan">Scan &amp; Preview</button>
        <button type="submit" style="background:#6b7280">Download CSV directly</button>
      </div>
    </form>
  </div>
</body>
</html>"""

# Hardcoded folder presets — add more as (label, folder_id) tuples
_FOLDER_PRESETS = [
    ("Project 1", "ab89661f-0b80-44ea-93f1-11968b96ac3d"),
]


def _seconds_to_timecode(seconds) -> str:
    if seconds is None:
        return ""
    s = int(float(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


@app.route('/comments', methods=['GET'])
def comments_ui():
    folder_id = request.args.get('folder_id', '')
    return render_template_string(_COMMENTS_UI, folder_id=folder_id, presets=_FOLDER_PRESETS)


def _comment_rows(file_name: str, file_id: str, comments: list) -> list[list]:
    """Flatten a list of comments (plus any nested replies) into CSV rows."""
    rows = []
    for c in comments:
        owner = c.get('owner') or {}
        author = owner.get('name') or owner.get('email') or owner.get('id') or 'Unknown'
        rows.append([
            file_name, file_id, author,
            c.get('text', ''),
            _seconds_to_timecode(c.get('timestamp')),
            c.get('created_at', ''),
            'Yes' if c.get('completed_at') else 'No',
            'No',
            '',
        ])
        for reply in c.get('replies', []):
            r_owner = reply.get('owner') or {}
            r_author = r_owner.get('name') or r_owner.get('email') or r_owner.get('id') or 'Unknown'
            rows.append([
                file_name, file_id, r_author,
                reply.get('text', ''),
                _seconds_to_timecode(reply.get('timestamp')),
                reply.get('created_at', ''),
                'Yes' if reply.get('completed_at') else 'No',
                'Yes',
                c.get('id', ''),
            ])
    return rows


_CSV_HEADERS = ['file_name', 'file_id', 'author', 'comment', 'timecode', 'created_at', 'completed', 'is_reply', 'parent_comment_id']


@app.route('/comments/debug-pagination', methods=['GET'])
def comments_debug_pagination():
    """Return the raw first-page response from the folder children endpoint (minus data items)."""
    from frameio_client import _api_call
    folder_id = request.args.get('folder_id', '').strip()
    if not folder_id:
        return 'Missing folder_id parameter', 400
    account_id = os.environ.get('FRAMEIO_ACCOUNT_ID', '')
    result = _api_call('GET', f'/accounts/{account_id}/folders/{folder_id}/children', params={'page_size': 50})
    # Return everything except the data array so the response is readable
    debug = {k: v for k, v in result.items() if k != 'data'}
    debug['data_count'] = len(result.get('data', []))
    debug['first_item_keys'] = list(result['data'][0].keys()) if result.get('data') else []
    return jsonify(debug), 200


@app.route('/comments/scan', methods=['GET'])
def comments_scan():
    """Stream an HTML progress page showing each file and its comment count."""
    from frameio_client import get_all_files_in_folder, get_file_comments
    folder_id = request.args.get('folder_id', '').strip()
    if not folder_id:
        return 'Missing folder_id parameter', 400
    account_id = os.environ.get('FRAMEIO_ACCOUNT_ID', '')
    if not account_id:
        return 'FRAMEIO_ACCOUNT_ID not configured', 500

    def generate():
        yield f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Scanning folder…</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;margin:0;padding:40px 20px}}
  .card{{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.1);max-width:700px;margin:0 auto;padding:32px}}
  h1{{font-size:1.2rem;margin:0 0 4px}}
  p.sub{{color:#666;font-size:.85rem;margin:0 0 24px}}
  table{{width:100%;border-collapse:collapse;font-size:.875rem}}
  th{{text-align:left;padding:6px 8px;border-bottom:2px solid #e5e7eb;color:#374151}}
  td{{padding:6px 8px;border-bottom:1px solid #f3f4f6;word-break:break-all}}
  .count{{text-align:right;font-weight:600}}
  .err{{color:#dc2626}}
  .summary{{margin-top:20px;font-weight:600;font-size:.95rem}}
  .dl{{display:inline-block;margin-top:16px;background:#2563eb;color:#fff;padding:10px 22px;
       border-radius:6px;text-decoration:none;font-weight:600}}
  .dl:hover{{background:#1d4ed8}}
</style></head><body><div class="card">
<h1>Scanning folder…</h1>
<p class="sub">Folder: {folder_id}</p>
<table><thead><tr><th>#</th><th>File</th><th>Type</th><th class="count">Comments</th></tr></thead><tbody>"""

        try:
            files = get_all_files_in_folder(account_id, folder_id)
        except Exception as e:
            yield f'<tr><td colspan="4" class="err">ERROR fetching folder: {e}</td></tr>'
            yield '</tbody></table></div></body></html>'
            return

        total_comments = 0
        total_replies = 0
        for i, f in enumerate(files, 1):
            fid = f.get('id', '')
            fname = f.get('name', 'Unknown')
            ftype = f.get('type', '')
            try:
                comments = get_file_comments(account_id, fid)
                n_comments = len(comments)
                n_replies = sum(len(c.get('replies', [])) for c in comments)
                total_comments += n_comments
                total_replies += n_replies
                count_str = f'{n_comments} comment{"s" if n_comments != 1 else ""}'
                if n_replies:
                    count_str += f' + {n_replies} repl{"ies" if n_replies != 1 else "y"}'
                yield f'<tr><td>{i}</td><td>{fname}</td><td>{ftype}</td><td class="count">{count_str}</td></tr>'
            except Exception as e:
                yield f'<tr><td>{i}</td><td>{fname}</td><td>{ftype}</td><td class="err">ERROR: {e}</td></tr>'

        yield f"""</tbody></table>
<p class="summary">Found {len(files)} file(s) — {total_comments} comment(s), {total_replies} repl(ies) total.</p>
<a class="dl" href="/comments/export?folder_id={folder_id}">Download CSV</a>
</div></body></html>"""

    return Response(generate(), mimetype='text/html')


@app.route('/comments/export', methods=['GET'])
def comments_export():
    from frameio_client import get_all_files_in_folder, get_file_comments
    folder_id = request.args.get('folder_id', '').strip()
    if not folder_id:
        return 'Missing folder_id parameter', 400

    account_id = os.environ.get('FRAMEIO_ACCOUNT_ID', '')
    if not account_id:
        return 'FRAMEIO_ACCOUNT_ID not configured', 500

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_HEADERS)
        yield buf.getvalue()

        try:
            files = get_all_files_in_folder(account_id, folder_id)
        except Exception as e:
            yield f"# ERROR fetching folder: {e}\n"
            return

        for f in files:
            file_id = f.get('id', '')
            file_name = f.get('name', '')
            try:
                comments = get_file_comments(account_id, file_id)
            except Exception as e:
                logger.warning(f"Could not fetch comments for {file_id}: {e}")
                continue

            for row in _comment_rows(file_name, file_id, comments):
                buf.seek(0)
                buf.truncate()
                writer.writerow(row)
                yield buf.getvalue()

    filename = f"comments_{folder_id[:8]}.csv"
    return Response(
        generate(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    return jsonify(status='ok'), 200