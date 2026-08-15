"""FastAPI application for rs3graph_webui.

Routes:
  /                       Market watch page
  /admin                  Admin panel
  /api/markets            CRUD markets
  /api/markets/reorder    Persist drag-and-drop market order
  /api/markets/{id}/items CRUD items inside a market
  /api/markets/{id}/items/reorder   Persist drag-and-drop item order
  /api/markets/{id}/settings        Lookback + auto-refresh interval
  /api/markets/{id}/items/{iid}/data   Chart-ready series for one item
  /api/markets/{id}/refresh            Kick off a background data refresh
  /api/jobs/{job_id}      Poll refresh progress
  /api/lookup/{iid}       Live name/icon lookup from the RS Wiki mapping
  /icons/{filename}       Cached item icons (downloaded on demand)
"""

import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api_client, db
from .models import (
    ItemAdd,
    ItemImport,
    ItemsReorder,
    LookupOut,
    MarketCreate,
    MarketReorder,
    MarketRename,
    MarketSettings,
)

# Load .env from the project root so RS3GRAPH_USER_AGENT (and any future
# settings) are picked up before the API client module reads them.
load_dotenv()

STATIC_DIR = Path(__file__).parent / "static"
SEED_ITEM_IDS = [4151, 49430, 1519]  # from the original items.txt

# ─────────────────────────────────────────────────────────────────────────────
#  Background refresh jobs (single in-memory registry; fine for one VPS user)
# ─────────────────────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
# market_id -> active job_id, guarded by _jobs_lock.  Used so the manual
# refresh button and the auto-refresh loop never run two refreshes of the
# same market at once.
_active_refresh: dict[int, str] = {}

# Refresh policy: items are fetched in waves of CHUNK_SIZE, each wave run
# concurrently by the API client's ThreadPoolExecutor (FETCH_CONCURRENCY
# workers).  The small pause between waves keeps the request rate polite.
REFRESH_CHUNK_SIZE = 20
REFRESH_CHUNK_DELAY_S = 0.25


def _set_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        _jobs[job_id].update(kwargs)


def _clear_active_refresh(market_id: int, job_id: str) -> None:
    """Drop the active-job marker if it still points at *job_id*."""
    with _jobs_lock:
        if _active_refresh.get(market_id) == job_id:
            del _active_refresh[market_id]


def _start_refresh_job(market_id: int) -> str:
    """Register and spawn a refresh job for *market_id*.

    If a refresh for this market is already queued/running, the existing
    job id is returned instead so callers (UI poll + auto-refresh loop)
    can both follow the same job.
    """
    with _jobs_lock:
        existing = _active_refresh.get(market_id)
        if existing and _jobs.get(existing, {}).get("state") in ("queued", "running"):
            return existing
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "total": 0,
            "done": 0,
            "current": "",
            "message": "",
            "market_id": market_id,
        }
        _active_refresh[market_id] = job_id
    threading.Thread(target=_run_refresh, args=(job_id, market_id), daemon=True).start()
    return job_id


def _auto_refresh_loop(stop_event: threading.Event) -> None:
    """Background loop driving the per-market auto-refresh feature.

    Wakes every 60 seconds and starts a refresh for every market whose
    ``update_interval_minutes`` has elapsed since ``last_refresh``.
    Markets with interval 0 (manual) are skipped.  The loop is defensive:
    any error is swallowed so it can never die and leave auto-refresh off.
    """
    while not stop_event.wait(60):
        try:
            for market in db.list_markets():
                interval = market.get("update_interval_minutes") or 0
                if interval <= 0:
                    continue
                last = market.get("last_refresh")
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last)
                    except ValueError:
                        last_dt = None
                    if last_dt is not None:
                        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        if elapsed < interval * 60:
                            continue
                # Never-refreshed markets with an interval are due immediately.
                _start_refresh_job(market["id"])
        except Exception:  # noqa: BLE001 - keep the loop alive at all costs
            continue


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_refresh(job_id: str, market_id: int) -> None:
    market = db.get_market(market_id)
    if not market:
        _set_job(job_id, state="error", message="Market not found")
        _clear_active_refresh(market_id, job_id)
        return

    items = db.list_items(market_id)
    total = len(items)
    if total == 0:
        db.set_meta(f"market:{market_id}:last_refresh", _now_iso())
        _set_job(job_id, state="done", total=0, done=0, current="",
                 message="Market has no items")
        _clear_active_refresh(market_id, job_id)
        return

    _set_job(job_id, state="running", total=total, done=0, current="")

    try:
        # Fetch exactly the market's configured window: a 24h market must
        # never pull 30d of data (and vice versa).
        lookback = market.get("lookback") or api_client.LOOKBACK
        lookback_hours = api_client.LOOKBACK_HOURS.get(lookback, 24)

        # Names/icons come from the mapping; a failure here is not fatal,
        # we just keep whatever is stored.
        try:
            mapping = api_client.fetch_mapping()
        except Exception:
            mapping = None

        cutoff = int(time.time()) - lookback_hours * 3600  # keep only the window

        done = 0
        for start in range(0, total, REFRESH_CHUNK_SIZE):
            chunk = items[start : start + REFRESH_CHUNK_SIZE]
            chunk_ids = [item["item_id"] for item in chunk]
            chunk_end = min(start + len(chunk), total)
            _set_job(
                job_id,
                done=done,
                current=f"Fetching {start + 1}-{chunk_end} of {total}…",
            )

            # All network I/O for the wave runs concurrently (max 10 in
            # flight); the DB writes below stay sequential and cheap.
            data_map, failures = api_client.fetch_timeseries_many(
                chunk_ids, lookback=lookback
            )
            if failures:
                _set_job(
                    job_id,
                    current=f"{len(failures)} item(s) failed to fetch, keeping old data…",
                )

            for item in chunk:
                item_id = item["item_id"]
                label = item["item_name"] or f"Item {item_id}"
                done += 1
                _set_job(job_id, done=done, current=label)

                # Skip failed fetches entirely so stored data is preserved.
                data = data_map.get(item_id)
                if data is None:
                    continue
                rows = [
                    (
                        e.get("timestamp"),
                        e.get("avgHighPrice"),
                        e.get("avgLowPrice"),
                        e.get("highPriceVolume"),
                        e.get("lowPriceVolume"),
                    )
                    for e in data
                ]
                db.replace_price_data(item_id, rows)
                db.cleanup_old(item_id, cutoff)

                # Keep stored names/icons in sync with the wiki mapping.
                if mapping and item_id in mapping:
                    m = mapping[item_id]
                    db.update_item_meta(market_id, item_id, m["name"], m["icon"] or None)
                    if m.get("icon"):
                        api_client.download_icon(m["icon"])

            if chunk_end < total:
                time.sleep(REFRESH_CHUNK_DELAY_S)  # be polite between waves

        db.set_meta(f"market:{market_id}:last_refresh", _now_iso())
        _set_job(job_id, state="done", done=total, current="",
                 message=f"Refreshed {total} item(s)")
    except Exception as exc:  # noqa: BLE001 - report any failure to the UI
        _set_job(job_id, state="error", current="", message=str(exc))
    finally:
        _clear_active_refresh(market_id, job_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Startup seeding
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_seed(market_id: int) -> None:
    """Fill in real names/icons for the seeded items in the background."""
    try:
        mapping = api_client.fetch_mapping(force=True)
    except Exception:
        return
    for item_id in SEED_ITEM_IDS:
        m = mapping.get(item_id)
        if m:
            db.update_item_meta(market_id, item_id, m["name"], m["icon"] or None)
            if m.get("icon"):
                api_client.download_icon(m["icon"])
            time.sleep(0.2)


def seed_default_market() -> None:
    """On first run, create the "Default" market with items from items.txt."""
    if db.list_markets():
        return
    try:
        market_id = db.create_market("Default")
    except ValueError:
        return
    for item_id in SEED_ITEM_IDS:
        db.add_item(market_id, item_id, f"Item {item_id}", None)
    threading.Thread(target=_enrich_seed, args=(market_id,), daemon=True).start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    seed_default_market()
    stop_event = threading.Event()
    threading.Thread(
        target=_auto_refresh_loop, args=(stop_event,), daemon=True
    ).start()
    yield
    stop_event.set()


app = FastAPI(title="RS3 Graph WebUI", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─────────────────────────────────────────────────────────────────────────────
#  Pages
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ─────────────────────────────────────────────────────────────────────────────
#  Markets
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/markets")
def list_markets():
    return db.list_markets()


@app.put("/api/markets/reorder")
def reorder_markets(body: MarketReorder):
    """Persist a new drag-and-drop order for the market list."""
    existing = {m["id"] for m in db.list_markets()}
    unknown = [mid for mid in body.market_ids if mid not in existing]
    if unknown:
        raise HTTPException(404, f"Unknown market id(s): {unknown}")
    db.reorder_markets(body.market_ids)
    return {"ok": True}


@app.post("/api/markets")
def create_market(body: MarketCreate):
    name = body.name.strip()
    try:
        market_id = db.create_market(name)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"id": market_id, "name": name, "item_count": 0, "last_refresh": None}


@app.put("/api/markets/{market_id}")
def rename_market(market_id: int, body: MarketRename):
    name = body.name.strip()
    try:
        db.rename_market(market_id, name)
    except KeyError:
        raise HTTPException(404, "Market not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"id": market_id, "name": name}


@app.delete("/api/markets/{market_id}")
def delete_market(market_id: int):
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")
    db.delete_market(market_id)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
#  Market items
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/markets/{market_id}/items")
def list_items(market_id: int):
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")
    return db.list_items(market_id)


@app.post("/api/markets/{market_id}/items")
def add_item(market_id: int, body: ItemAdd):
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")
    if db.get_market_item(market_id, body.item_id):
        raise HTTPException(409, "Item already in this market")

    try:
        mapping = api_client.fetch_mapping()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch item mapping: {exc}")

    meta = mapping.get(body.item_id)
    if not meta:
        raise HTTPException(404, f"Unknown item ID {body.item_id}")

    db.add_item(market_id, body.item_id, meta["name"], meta["icon"] or None)
    if meta.get("icon"):
        api_client.download_icon(meta["icon"])  # warm the cache
    return {
        "item_id": body.item_id,
        "item_name": meta["name"],
        "icon_name": meta["icon"] or None,
    }


@app.put("/api/markets/{market_id}/items/reorder")
def reorder_items(market_id: int, body: ItemsReorder):
    """Persist a new drag-and-drop order for the items in one market."""
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")
    existing = {it["item_id"] for it in db.list_items(market_id)}
    unknown = [iid for iid in body.item_ids if iid not in existing]
    if unknown:
        raise HTTPException(404, f"Unknown item id(s) in this market: {unknown}")
    db.reorder_items(market_id, body.item_ids)
    return {"ok": True}


@app.put("/api/markets/{market_id}/settings")
def update_market_settings(market_id: int, body: MarketSettings):
    """Update per-market lookback / auto-refresh interval."""
    try:
        db.update_market_settings(
            market_id,
            lookback=body.lookback,
            update_interval_minutes=body.update_interval_minutes,
        )
    except KeyError:
        raise HTTPException(404, "Market not found")
    return db.get_market(market_id)


@app.post("/api/markets/{market_id}/import")
def import_items(market_id: int, body: ItemImport):
    """Bulk-import item IDs into a market, in the order given.

    Each ID is looked up via the wiki mapping; only valid, not-yet-present
    items are added.  The display order always follows the input list order.
    Returns a per-category summary.
    """
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")

    try:
        mapping = api_client.fetch_mapping()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch item mapping: {exc}")

    added: list[int] = []
    skipped: list[int] = []
    not_found: list[int] = []

    for item_id in body.item_ids:
        if item_id <= 0:
            not_found.append(item_id)
            continue
        if db.get_market_item(market_id, item_id):
            skipped.append(item_id)
            continue
        meta = mapping.get(item_id)
        if not meta:
            not_found.append(item_id)
            continue
        db.add_item(market_id, item_id, meta["name"], meta["icon"] or None)
        if meta.get("icon"):
            api_client.download_icon(meta["icon"])  # warm the cache
        added.append(item_id)

    return {"added": added, "skipped": skipped, "not_found": not_found}


@app.delete("/api/markets/{market_id}/items/{item_id}")
def remove_item(market_id: int, item_id: int):
    if not db.get_market_item(market_id, item_id):
        raise HTTPException(404, "Item not in this market")
    db.remove_item(market_id, item_id)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
#  Chart data
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/markets/{market_id}/items/{item_id}/data")
def item_data(market_id: int, item_id: int, lookback: str = ""):
    item = db.get_market_item(market_id, item_id)
    if not item:
        raise HTTPException(404, "Item not in this market")

    # Effective lookback: an explicit ?lookback= wins; otherwise fall back
    # to the market's configured window so old API consumers keep working.
    if lookback:
        if lookback not in api_client.LOOKBACK_HOURS:
            raise HTTPException(
                400, f"Invalid lookback {lookback!r}; choose from "
                     f"{sorted(api_client.LOOKBACK_HOURS)}"
            )
    else:
        market = db.get_market(market_id)
        lookback = (market or {}).get("lookback") or api_client.LOOKBACK
    hours = api_client.LOOKBACK_HOURS[lookback]

    # Lazily heal missing icons so images always appear once cached.
    if item["icon_name"]:
        api_client.download_icon(item["icon_name"])

    return {
        "item_id": item_id,
        "item_name": item["item_name"],
        "icon_name": item["icon_name"],
        "period": lookback,
        "stats": db.stats_for(item_id, hours),
        "series": db.get_series(item_id, hours),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Lookup / refresh jobs
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/lookup/{item_id}", response_model=LookupOut)
def lookup_item(item_id: int):
    try:
        mapping = api_client.fetch_mapping()
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch item mapping: {exc}")
    meta = mapping.get(item_id)
    if not meta:
        raise HTTPException(404, f"Unknown item ID {item_id}")
    if meta.get("icon"):
        api_client.download_icon(meta["icon"])
    return {"item_id": item_id, "name": meta["name"], "icon": meta["icon"] or None}


@app.post("/api/markets/{market_id}/refresh")
def refresh_market(market_id: int):
    if not db.get_market(market_id):
        raise HTTPException(404, "Market not found")
    job_id = _start_refresh_job(market_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


# ─────────────────────────────────────────────────────────────────────────────
#  Icons (cached locally, downloaded on demand)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/icons/{filename}")
def get_icon(filename: str):
    safe = Path(filename).name  # block path traversal
    if safe != filename:
        raise HTTPException(400, "Invalid icon path")

    api_client.download_icon(safe)
    path = db.ICONS_DIR / api_client.icon_filename(safe)
    if path.exists():
        return FileResponse(path, media_type="image/png")
    raise HTTPException(404, "Icon not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
