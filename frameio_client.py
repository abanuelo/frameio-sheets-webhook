"""Wraps Frame.io v4 API access using OAuth refresh token flow."""
import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

ADOBE_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_API_BASE = "https://api.frame.io/v4"

_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    response = requests.post(
        ADOBE_IMS_TOKEN_URL,
        data={
            'grant_type': 'refresh_token',
            'client_id': os.environ['ADOBE_CLIENT_ID'],
            'client_secret': os.environ['ADOBE_CLIENT_SECRET'],
            'refresh_token': os.environ['ADOBE_REFRESH_TOKEN'],
        },
        timeout=10,
    )
    
    if response.status_code != 200:
        logger.error(f"Token refresh failed: {response.status_code} {response.text}")
        raise RuntimeError(f"Failed to refresh access token: {response.text}")

    data = response.json()
    _token_cache["access_token"] = data['access_token']
    _token_cache["expires_at"] = time.time() + data['expires_in']
    
    new_refresh = data.get('refresh_token')
    if new_refresh and new_refresh != os.environ['ADOBE_REFRESH_TOKEN']:
        logger.warning("Adobe rotated the refresh token. Update ADOBE_REFRESH_TOKEN to: %s", new_refresh)
    
    return _token_cache["access_token"]


def _api_call(method: str, path: str, **kwargs):
    token = get_access_token()
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {token}'
    headers.setdefault('Accept', 'application/json')

    url = f"{FRAMEIO_API_BASE}{path}"

    for attempt in range(4):
        response = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        if response.status_code == 401 and attempt == 0:
            _token_cache["access_token"] = None
            _token_cache["expires_at"] = 0
            token = get_access_token()
            headers['Authorization'] = f'Bearer {token}'
            continue

        if response.status_code == 429:
            wait = int(response.headers.get('Retry-After', 10))
            logger.warning(f"Rate limited by Frame.io — waiting {wait}s before retry")
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response.json()

    response.raise_for_status()
    return response.json()


def get_file(account_id: str, file_id: str) -> dict:
    """Fetch a file with metadata included."""
    result = _api_call(
        'GET',
        f'/accounts/{account_id}/files/{file_id}',
        params={'include': 'metadata'}
    )
    return result.get('data', {})


def parse_metadata(file_data: dict) -> dict:
    """
    Parse Frame.io's metadata array into a flat dict keyed by field_definition_name.
    
    Handles different field types:
    - select: extracts display_name from value[0]
    - text/number/toggle: returns value as-is
    - user_single/user_multi: returns list of user IDs (caller can resolve to names)
    """
    metadata_array = file_data.get('metadata', [])
    parsed = {}
    
    for field in metadata_array:
        name = field.get('field_definition_name')
        if not name:
            continue
        
        ftype = field.get('field_type')
        value = field.get('value')
        
        if ftype == 'select':
            # value is a list of {display_name, id} objects
            if isinstance(value, list) and value:
                parsed[name] = value[0].get('display_name', '')
            else:
                parsed[name] = ''
        elif ftype in ('user_single', 'user_multi'):
            # value is a list of {id, type} - caller needs to resolve to names
            if isinstance(value, list):
                parsed[name] = [u.get('id') for u in value if u.get('id')]
            else:
                parsed[name] = []
        else:
            # text, number, toggle, date - take value as-is
            parsed[name] = value if value is not None else ''
    
    return parsed

def get_project(account_id: str, project_id: str) -> dict:
    """Fetch a project by ID."""
    result = _api_call('GET', f'/accounts/{account_id}/projects/{project_id}')
    return result.get('data', {})


def _has_more_pages(result: dict, page: list) -> bool:
    """Return True if the API response indicates there are more pages to fetch."""
    if not page:
        return False
    # Cursor-based signals (check all known locations)
    cursor = (
        result.get('next_cursor')
        or result.get('pagination', {}).get('next_cursor')
        or result.get('pagination', {}).get('cursor')
        or result.get('meta', {}).get('next_cursor')
        or result.get('page_info', {}).get('end_cursor')
    )
    if cursor:
        return True
    # Boolean signals
    if result.get('has_more') or result.get('pagination', {}).get('has_more'):
        return True
    return False


def _next_cursor(result: dict) -> str | None:
    """Extract the pagination cursor from a Frame.io v4 response regardless of where it lives."""
    return (
        result.get('next_cursor')
        or result.get('pagination', {}).get('next_cursor')
        or result.get('pagination', {}).get('cursor')
        or result.get('meta', {}).get('next_cursor')
        or result.get('page_info', {}).get('end_cursor')
        or None
    )


def get_folder_children(account_id: str, folder_id: str) -> list:
    """Return direct children of a folder (files + sub-folders), fully paginated."""
    children = []
    cursor = None
    while True:
        params = {'page_size': 50}
        if cursor:
            params['after'] = cursor
        result = _api_call('GET', f'/accounts/{account_id}/folders/{folder_id}/children', params=params)
        page = result.get('data', [])
        children.extend(page)
        if not _has_more_pages(result, page):
            break
        cursor = _next_cursor(result)
        if not cursor:
            break
    return children


def get_all_files_in_folder(account_id: str, folder_id: str) -> list:
    """Recursively return every non-folder asset under a folder."""
    files = []
    for item in get_folder_children(account_id, folder_id):
        if item.get('type') == 'folder':
            files.extend(get_all_files_in_folder(account_id, item['id']))
        else:
            files.append(item)
    return files


def get_file_comments(account_id: str, file_id: str) -> list:
    """Return all top-level comments (with replies and owner) for a file, fully paginated."""
    comments = []
    cursor = None
    while True:
        params = {'page_size': 50, 'include': 'owner,replies'}
        if cursor:
            params['after'] = cursor
        result = _api_call('GET', f'/accounts/{account_id}/files/{file_id}/comments', params=params)
        page = result.get('data', [])
        comments.extend(page)
        if not _has_more_pages(result, page):
            break
        cursor = _next_cursor(result)
        if not cursor:
            break
    return comments