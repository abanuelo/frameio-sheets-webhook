"""Persists the Adobe refresh token in Vercel KV (Upstash Redis REST API).

Falls back to the ADOBE_REFRESH_TOKEN env var when KV is not configured
(local dev) or the key doesn't exist yet (first run after enabling KV).
"""
import os
import logging
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_KEY = "adobe_refresh_token"
_cache = {"token": None}

# ponytail: last-write-wins on concurrent rotation; fine at daily-cron
# frequency, add a Redis lock if refreshes ever race.


def _kv_url():
    return os.environ.get('KV_REST_API_URL')


def _kv_headers() -> dict:
    return {'Authorization': f"Bearer {os.environ['KV_REST_API_TOKEN']}"}


def get_refresh_token() -> str:
    if _cache["token"]:
        return _cache["token"]

    if _kv_url():
        try:
            resp = requests.get(f"{_kv_url()}/get/{_KEY}", headers=_kv_headers(), timeout=10)
            resp.raise_for_status()
            token = resp.json().get('result')
            if token:
                _cache["token"] = token
                return token
            logger.info("No refresh token in KV yet — bootstrapping from ADOBE_REFRESH_TOKEN env var")
        except Exception:
            logger.exception("KV read failed — falling back to ADOBE_REFRESH_TOKEN env var")
    else:
        logger.info("KV_REST_API_URL not set — using ADOBE_REFRESH_TOKEN env var only")

    _cache["token"] = os.environ['ADOBE_REFRESH_TOKEN']
    return _cache["token"]


def save_refresh_token(token: str) -> None:
    _cache["token"] = token
    if not _kv_url():
        logger.warning("KV not configured — rotated refresh token NOT persisted. "
                       "Update ADOBE_REFRESH_TOKEN manually to: %s", token)
        return
    try:
        resp = requests.get(f"{_kv_url()}/set/{_KEY}/{quote(token, safe='')}",
                            headers=_kv_headers(), timeout=10)
        resp.raise_for_status()
        logger.info("Persisted rotated refresh token to KV")
    except Exception:
        logger.exception("Failed to persist rotated refresh token to KV — "
                         "next cold start will use a stale token. New token: %s", token)
