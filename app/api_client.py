"""RS Wiki Prices API client.

Reuses the logic from the original `generate_graphs.py` (mapping fetch,
timeseries fetch, icon caching with spaces -> underscores, and the 0.5 s
request delay between calls).
"""

import threading
import time
from pathlib import Path

import requests

from . import db

API_BASE = "https://prices.runescape.wiki/api/v2/rs"
WIKI_IMG_BASE = "https://runescape.wiki/images"
USER_AGENT = "rs3graph-webui/1.0 (RuneScape price tracker; contact: local)"
LOOKBACK = "24h"
REQUEST_DELAY_S = 0.5

# Item-ID -> {name, icon} mapping, cached in memory for a while.
MAPPING_TTL_S = 6 * 3600
_mapping: dict | None = None
_mapping_fetched_at = 0.0
_mapping_lock = threading.Lock()

_icon_lock = threading.Lock()


def fetch_mapping(force: bool = False) -> dict:
    """Return {item_id: {"name": str, "icon": str}} from /mapping."""
    global _mapping, _mapping_fetched_at
    now = time.time()
    with _mapping_lock:
        if not force and _mapping is not None and now - _mapping_fetched_at < MAPPING_TTL_S:
            return _mapping

        url = f"{API_BASE}/mapping"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        _mapping = {
            item["id"]: {
                "name": item["name"],
                "icon": item.get("icon") or "",
            }
            for item in resp.json()
        }
        _mapping_fetched_at = now
        return _mapping


def fetch_timeseries(item_id: int) -> list[dict]:
    """Fetch 24h timeseries entries for *item_id* (list of raw API records)."""
    url = f"{API_BASE}/timeseries"
    params = {"lookback": LOOKBACK, "id": str(item_id)}
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def icon_filename(icon_name: str) -> str:
    """Local cache filename for an icon (spaces -> underscores)."""
    safe = icon_name.replace(" ", "_") if icon_name else "unknown.png"
    return safe


def download_icon(icon_name: str, force: bool = False) -> Path | None:
    """Download and cache an icon from the RuneScape Wiki.

    Returns the local path (or None on failure / unknown icon).
    """
    if not icon_name:
        return None

    safe = icon_filename(icon_name)
    local = db.ICONS_DIR / safe
    if local.exists() and not force:
        return local

    with _icon_lock:
        if local.exists() and not force:
            return local
        db.ICONS_DIR.mkdir(parents=True, exist_ok=True)

        url = f"{WIKI_IMG_BASE}/{safe}"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            local.write_bytes(resp.content)
            return local
        except requests.RequestException:
            return None
