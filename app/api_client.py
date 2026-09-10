"""RS Wiki Prices API client.

Reuses the logic from the original `generate_graphs.py` (mapping fetch,
timeseries fetch, icon caching with spaces -> underscores).  Timeseries
fetches for a refresh run concurrently via ThreadPoolExecutor (the API does
not accept multiple ids per request), so no per-request sleep is needed.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests

from . import db

API_BASE = "https://prices.runescape.wiki/api/v2/rs"
WIKI_IMG_BASE = "https://runescape.wiki/images"
# Weirdgloop's exchange API serves the game's "market price" (the trading
# post's guide price, distinct from the wiki's buy/sell averages).
WEIRDLOOP_BASE = "https://api.weirdgloop.org/exchange/history/rs"
# The latest endpoint takes repeated names joined by a pipe; chunk long
# item lists so the URL stays a sane length.
WEIRDLOOP_CHUNK_SIZE = 50
# User-Agent sent with every request to the RS Wiki API; override via the
# RS3GRAPH_USER_AGENT environment variable (see .env / README.md).
USER_AGENT = os.environ.get(
    "RS3GRAPH_USER_AGENT", "rs3graph-webui/1.0 (RuneScape price tracker)"
)
LOOKBACK = "24h"

# The timeseries endpoint takes ONE id per request (repeated id= params are
# rejected with 400), so refreshes fetch items concurrently instead.  This is
# the number of simultaneous requests we keep in flight; well within the
# wiki's politeness guidelines while being far faster than the old fully
# sequential 0.5 s-per-item crawl.
FETCH_CONCURRENCY = 10

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


def fetch_timeseries_many(
    item_ids: list[int], max_workers: int = FETCH_CONCURRENCY
) -> tuple[dict[int, list[dict]], dict[int, Exception]]:
    """Fetch timeseries for many item IDs concurrently.

    The RS Wiki prices API does NOT support batching multiple ids in one
    timeseries request, so we parallelize individual requests with a
    ThreadPoolExecutor capped at *max_workers* (default 10) to stay polite.

    Returns ``(results, failures)`` where results maps item_id -> raw API
    records and failures maps item_id -> the exception raised for that item.
    A failed item is simply absent from ``results`` so callers can keep the
    previously stored data instead of wiping it.
    """
    results: dict[int, list[dict]] = {}
    failures: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_timeseries, iid): iid for iid in item_ids}
        for future in as_completed(futures):
            iid = futures[future]
            try:
                results[iid] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad item must not kill the batch
                failures[iid] = exc
    return results, failures


def fetch_weirdgloop_prices(item_names: list[str]) -> dict[str, dict]:
    """Fetch the latest Weirdgloop exchange price for many item names.

    Calls ``/exchange/history/rs/latest?name=A|B|C`` (names percent-encoded
    and joined by %7C) and returns ``{item_name: {"price": float,
    "volume": int}}``.  Names the API does not know about are simply absent
    from the result, and a failed chunk contributes no entries rather than
    blowing up the whole call — the caller degrades to "no market price".

    Requests are chunked (WEIRDLOOP_CHUNK_SIZE names each) to keep the URL
    short.
    """
    names = [n for n in dict.fromkeys(item_names) if n]  # de-dup, keep order
    prices: dict[str, dict] = {}

    for start in range(0, len(names), WEIRDLOOP_CHUNK_SIZE):
        chunk = names[start : start + WEIRDLOOP_CHUNK_SIZE]
        # Percent-encode each name ourselves and join with %7C so the pipe
        # separator (and spaces etc. inside names) is escaped exactly once.
        query = "%7C".join(quote(n, safe="") for n in chunk)
        url = f"{WEIRDLOOP_BASE}/latest?name={query}"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            continue

        # The endpoint normally returns {name: {price, volume, timestamp}};
        # tolerate a {"data": {...}} wrapper too.
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            continue

        for name, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            price = entry.get("price")
            if price is None:
                continue
            volume = entry.get("volume")
            prices[name] = {
                "price": float(price),
                "volume": int(volume) if volume is not None else 0,
            }

    return prices


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
