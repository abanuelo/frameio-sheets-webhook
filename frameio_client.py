"""Wraps Frame.io v4 API access using OAuth refresh token flow."""
import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

ADOBE_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_API_BASE = "https://api.frame.io/v4"

# Cached across warm invocations on Vercel
_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """Exchange refresh_token for short-lived access_token, with caching."""
    # Return cached token if still valid (with 60s buffer)
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    client_id = os.environ['ADOBE_CLIENT_ID']
    client_secret = os.environ['ADOBE_CLIENT_SECRET']
    refresh_token = os.environ['ADOBE_REFRESH_TOKEN']

    response = requests.post(
        ADOBE_IMS_TOKEN_URL,
        data={
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
        },
        timeout=10,
    )
    
    if response.status_code != 200:
        logger.error(f"Token refresh failed: {response.status_code} {response.text}")
        # If refresh token is invalid, this is fatal - need manual re-consent
        raise RuntimeError(f"Failed to refresh access token: {response.text}")

    data = response.json()
    
    _token_cache["access_token"] = data['access_token']
    _token_cache["expires_at"] = time.time() + data['expires_in']
    
    # IMPORTANT: Adobe sometimes returns a new refresh_token
    new_refresh = data.get('refresh_token')
    if new_refresh and new_refresh != refresh_token:
        # Log loudly - you need to update the env var manually
        logger.warning(
            "Adobe rotated the refresh token. "
            "Update ADOBE_REFRESH_TOKEN in Vercel to: %s",
            new_refresh
        )
    
    logger.info(f"Got new access token, expires in {data['expires_in']}s")
    return _token_cache["access_token"]


def _api_call(method: str, path: str, **kwargs):
    """Make an authenticated API call to Frame.io."""
    token = get_access_token()
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {token}'
    headers.setdefault('Accept', 'application/json')
    
    url = f"{FRAMEIO_API_BASE}{path}"
    response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
    
    if response.status_code == 401:
        # Access token might be stale — invalidate cache and retry once
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
        token = get_access_token()
        headers['Authorization'] = f'Bearer {token}'
        response = requests.request(method, url, headers=headers, timeout=10, **kwargs)
    
    response.raise_for_status()
    return response.json()


def get_accounts():
    """List accounts the authenticated user has access to."""
    return _api_call('GET', '/accounts')


def get_file(account_id: str, file_id: str, include: str = 'metadata') -> dict:
    """Fetch a file with optional includes (metadata, media_links, etc.)."""
    result = _api_call(
        'GET',
        f'/accounts/{account_id}/files/{file_id}',
        params={'include': include} if include else {}
    )
    return result.get('data', {})


def get_file_metadata(account_id: str, file_id: str) -> dict:
    """Fetch custom metadata fields for a file."""
    try:
        result = _api_call('GET', f'/accounts/{account_id}/files/{file_id}/metadata')
        return result.get('data', {})
    except requests.HTTPError as e:
        if e.response.status_code == 500:
            logger.warning(f"500 fetching metadata for {file_id}, returning empty")
            return {}
        raise

