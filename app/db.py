"""SQLite storage layer for rs3graph_webui.

Single-file, zero-config database.  The three core tables match the project
spec exactly.  An extra `meta` key/value table stores per-market
"last refreshed" timestamps (not part of the core schema, but the UI needs
them to show how fresh the data is).

SQLite connections are shared behind a re-entrant lock so FastAPI's thread
pool and the background refresh thread can all use the same file safely.
"""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ICONS_DIR = BASE_DIR / "icons"
DB_PATH = DATA_DIR / "rs3graph.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP,
    sort_order INTEGER DEFAULT 0,
    lookback TEXT NOT NULL DEFAULT '24h',
    update_interval_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS market_items (
    id INTEGER PRIMARY KEY,
    market_id INTEGER,
    item_id INTEGER NOT NULL,
    item_name TEXT,
    icon_name TEXT,
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY(market_id) REFERENCES markets(id),
    UNIQUE(market_id, item_id)
);

CREATE TABLE IF NOT EXISTS price_data (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    avg_high_price REAL,
    avg_low_price REAL,
    high_volume INTEGER,
    low_volume INTEGER,
    FOREIGN KEY(item_id) REFERENCES market_items(item_id)
);

CREATE INDEX IF NOT EXISTS idx_price_data_item_ts ON price_data(item_id, timestamp);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    """Ensure directories exist and the schema is created."""
    get_conn()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ICONS_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate()
        _conn.commit()
    return _conn


def _migrate() -> None:
    """Add columns that are missing from older databases.

    - ``market_items.sort_order``: display order inside a market (existing
      rows initialized from their rowid so the current visual order, which
      was previously ORDER BY item_id, is preserved).
    - ``markets.sort_order``: display order of the market list (existing
      rows initialized from their id, which was the previous sort key).
    - ``markets.lookback``: how much history the refresh job fetches
      ('24h' | '7d' | '30d').
    - ``markets.update_interval_minutes``: auto-refresh cadence
      (0 = manual, 5/15/30/60 = minutes).

    New rows get explicit values from create_market() / add_item().
    """
    item_cols = {row["name"] for row in _conn.execute("PRAGMA table_info(market_items)")}
    if "sort_order" not in item_cols:
        _conn.execute("ALTER TABLE market_items ADD COLUMN sort_order INTEGER DEFAULT 0")
        # Preserve the pre-migration visual order (previously ORDER BY item_id).
        _conn.execute(
            """
            UPDATE market_items
            SET sort_order = (
                SELECT COUNT(*) FROM market_items AS mi2
                WHERE mi2.market_id = market_items.market_id
                  AND (mi2.sort_order, mi2.rowid)
                      <= (market_items.sort_order, market_items.rowid)
            )
            """
        )

    mkt_cols = {row["name"] for row in _conn.execute("PRAGMA table_info(markets)")}
    if "sort_order" not in mkt_cols:
        _conn.execute("ALTER TABLE markets ADD COLUMN sort_order INTEGER DEFAULT 0")
        # Preserve the pre-migration order (previously ORDER BY id).
        _conn.execute("UPDATE markets SET sort_order = id WHERE sort_order = 0")
    if "lookback" not in mkt_cols:
        _conn.execute("ALTER TABLE markets ADD COLUMN lookback TEXT NOT NULL DEFAULT '24h'")
    if "update_interval_minutes" not in mkt_cols:
        _conn.execute(
            "ALTER TABLE markets ADD COLUMN update_interval_minutes INTEGER NOT NULL DEFAULT 0"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Markets
# ─────────────────────────────────────────────────────────────────────────────

def list_markets() -> list[dict]:
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM markets ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        out = []
        for r in rows:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM market_items WHERE market_id=?",
                (r["id"],),
            ).fetchone()["c"]
            out.append({
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"],
                "sort_order": r["sort_order"],
                "lookback": r["lookback"],
                "update_interval_minutes": r["update_interval_minutes"],
                "item_count": cnt,
                "last_refresh": get_meta(f"market:{r['id']}:last_refresh"),
            })
        return out


def reorder_markets(ordered_ids: list[int]) -> None:
    """Apply a new display order to the market list.

    *ordered_ids* is the full desired ordering.  Any market not listed is
    pushed after the listed ones, preserving its relative id order, so an
    incomplete payload can never silently reorder (or hide) other markets.
    """
    conn = get_conn()
    with _lock:
        for idx, market_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE markets SET sort_order=? WHERE id=?", (idx, market_id)
            )
        conn.execute(
            "UPDATE markets SET sort_order = ? + id "
            "WHERE id NOT IN ({})".format(
                ",".join("?" for _ in ordered_ids) or "NULL"
            ),
            (len(ordered_ids), *ordered_ids),
        )
        conn.commit()


def update_market_settings(
    market_id: int,
    lookback: str | None = None,
    update_interval_minutes: int | None = None,
) -> None:
    """Update per-market fetch/view settings; None leaves a field untouched."""
    conn = get_conn()
    sets = []
    params: list = []
    if lookback is not None:
        sets.append("lookback=?")
        params.append(lookback)
    if update_interval_minutes is not None:
        sets.append("update_interval_minutes=?")
        params.append(update_interval_minutes)
    if not sets:
        return
    params.append(market_id)
    with _lock:
        cur = conn.execute(
            f"UPDATE markets SET {', '.join(sets)} WHERE id=?", params
        )
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError("market not found")


def get_market(market_id: int) -> dict | None:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM markets WHERE id=?", (market_id,)
        ).fetchone()
        return dict(row) if row else None


def create_market(name: str) -> int:
    conn = get_conn()
    with _lock:
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM markets"
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO markets (name, created_at, sort_order) VALUES (?, ?, ?)",
                (name, datetime.now(timezone.utc).isoformat(), row["next"]),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Market named {name!r} already exists")


def rename_market(market_id: int, name: str) -> None:
    conn = get_conn()
    with _lock:
        try:
            cur = conn.execute(
                "UPDATE markets SET name=? WHERE id=?", (name, market_id)
            )
            conn.commit()
            if cur.rowcount == 0:
                raise KeyError("market not found")
        except sqlite3.IntegrityError:
            raise ValueError(f"Market named {name!r} already exists")


def delete_market(market_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM markets WHERE id=?", (market_id,))
        conn.execute("DELETE FROM market_items WHERE market_id=?", (market_id,))
        # Drop price data that no market references any more.
        conn.execute(
            "DELETE FROM price_data WHERE item_id NOT IN "
            "(SELECT DISTINCT item_id FROM market_items)"
        )
        conn.execute("DELETE FROM meta WHERE key=?", (f"market:{market_id}:last_refresh",))
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  Market items
# ─────────────────────────────────────────────────────────────────────────────

def list_items(market_id: int) -> list[dict]:
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            """
            SELECT mi.item_id, mi.item_name, mi.icon_name,
                   EXISTS(SELECT 1 FROM price_data pd
                          WHERE pd.item_id = mi.item_id) AS has_data
            FROM market_items mi
            WHERE mi.market_id = ?
            ORDER BY mi.sort_order ASC, mi.id ASC
            """,
            (market_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_market_item(market_id: int, item_id: int) -> dict | None:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT * FROM market_items WHERE market_id=? AND item_id=?",
            (market_id, item_id),
        ).fetchone()
        return dict(row) if row else None


def add_item(market_id: int, item_id: int, name: str, icon: str | None) -> None:
    """Insert *item_id* at the end of the market's display order.

    sort_order is MAX(sort_order)+1 per market so items appear in the exact
    order they were added.  A duplicate (same market+item) is ignored.
    """
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM market_items "
            "WHERE market_id=?",
            (market_id,),
        ).fetchone()
        next_order = row["next"]
        conn.execute(
            "INSERT OR IGNORE INTO market_items "
            "(market_id, item_id, item_name, icon_name, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (market_id, item_id, name, icon, next_order),
        )
        conn.commit()


def remove_item(market_id: int, item_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM market_items WHERE market_id=? AND item_id=?",
            (market_id, item_id),
        )
        # Only drop price data when no other market still tracks this item.
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM market_items WHERE item_id=?",
            (item_id,),
        ).fetchone()["c"]
        if cnt == 0:
            conn.execute("DELETE FROM price_data WHERE item_id=?", (item_id,))
        conn.commit()


def reorder_items(market_id: int, ordered_ids: list[int]) -> None:
    """Apply a new display order to the items inside *market_id*.

    Any item of this market not listed keeps a slot after the listed ones
    (in id order), so partial payloads cannot silently reorder or hide items.
    Items of other markets are never touched.
    """
    conn = get_conn()
    with _lock:
        for idx, item_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE market_items SET sort_order=? "
                "WHERE market_id=? AND item_id=?",
                (idx, market_id, item_id),
            )
        conn.execute(
            "UPDATE market_items SET sort_order = ? + id "
            "WHERE market_id=? AND item_id NOT IN ({})".format(
                ",".join("?" for _ in ordered_ids) or "NULL"
            ),
            (len(ordered_ids), market_id, *ordered_ids),
        )
        conn.commit()


def update_item_meta(market_id: int, item_id: int, name: str, icon: str | None) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE market_items SET item_name=?, icon_name=? "
            "WHERE market_id=? AND item_id=?",
            (name, icon, market_id, item_id),
        )
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  Price data
# ─────────────────────────────────────────────────────────────────────────────

def replace_price_data(item_id: int, rows: list[tuple]) -> None:
    """Replace all stored points for *item_id* with *rows*.

    Each row is (timestamp, avg_high, avg_low, high_volume, low_volume) in
    the same order the API returned them.  A refresh replaces the whole 24h
    window, so delete-then-insert is both correct and idempotent.
    """
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM price_data WHERE item_id=?", (item_id,))
        conn.executemany(
            "INSERT INTO price_data (item_id, timestamp, avg_high_price, avg_low_price, "
            "high_volume, low_volume) VALUES (?, ?, ?, ?, ?, ?)",
            [(item_id, *row) for row in rows],
        )
        conn.commit()


def cleanup_old(item_id: int, cutoff_ts: int) -> None:
    """Delete records older than *cutoff_ts* (unix seconds) for *item_id*."""
    conn = get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM price_data WHERE item_id=? AND timestamp < ?",
            (item_id, cutoff_ts),
        )
        conn.commit()


def fill_gaps(series: list) -> list:
    """Replicate the Python script's `_fill_gaps`.

    Forward-fill missing values, then back-fill leading gaps so the series is
    continuous from the first known value to the last known value.  Used for
    volumes: a genuinely missing slot carries the last known value.
    """
    filled: list = []
    last = None
    for v in series:
        if v is not None:
            last = v
            filled.append(v)
        else:
            filled.append(last)

    first_idx = next((i for i, v in enumerate(filled) if v is not None), None)
    if first_idx is not None and first_idx > 0:
        first_val = filled[first_idx]
        for i in range(first_idx):
            filled[i] = first_val

    return filled


def interp_gaps(series: list, timestamps: list | None = None) -> list:
    """Gap-fill a price series with linear interpolation in TIME space.

    Missing values between two real data points are placed ON the straight
    line connecting them (in (time, value) space), so the rendered line
    goes directly from point to point with the correct slope instead of
    going flat and then snapping to the new value (the old forward-fill
    behavior).  Leading gaps are back-filled with the first real value and
    trailing gaps keep the last real value (there is no second endpoint to
    interpolate toward yet).

    Interpolation is weighted by *timestamps*, not by array index: the DB
    can be missing entire 5-minute slots (irregular spacing), and
    index-based interpolation would place the filled values at the wrong
    time positions, making the drawn line kink/curve instead of running
    straight from point to point.  Falls back to index spacing if no
    timestamps are supplied.
    """
    n = len(series)
    out = list(series)

    first_idx = next((i for i, v in enumerate(out) if v is not None), None)
    if first_idx is None:
        return out  # nothing real at all — leave as-is
    for i in range(first_idx):
        out[i] = out[first_idx]  # back-fill leading gap

    prev = first_idx
    for i in range(first_idx + 1, n):
        if out[i] is None:
            continue
        if i > prev + 1:
            # Linearly interpolate every slot between the two real points,
            # weighted by time so each filled value sits exactly on the
            # straight line between the endpoints.
            a, b = out[prev], out[i]
            if timestamps is not None and timestamps[i] != timestamps[prev]:
                for j in range(prev + 1, i):
                    frac = ((timestamps[j] - timestamps[prev])
                            / (timestamps[i] - timestamps[prev]))
                    out[j] = a + (b - a) * frac
            else:
                step = (b - a) / (i - prev)
                for j in range(prev + 1, i):
                    out[j] = a + step * (j - prev)
        prev = i

    for i in range(prev + 1, n):
        out[i] = out[prev]  # trailing gap keeps the last real value

    return out


def get_series(item_id: int, lookback_hours: int | None = None) -> dict:
    """Return chart-ready parallel arrays for *item_id*.

    When *lookback_hours* is given, only data points within the last
    ``lookback_hours`` (relative to the wall clock) are returned; None
    returns everything stored.  The refresh job stores exactly the market's
    configured window, so the filter mainly serves the 24h/7d/30d view
    selector (and keeps charts stable while a wider window is still filling).

    - timestamps: epoch ms (EST rendering happens client-side)
    - highPrice / lowPrice: gap-filled for continuous lines; gaps between
      real points are linearly interpolated so lines run straight from
      point to point (no flat-then-snap)
    - highVolume / lowVolume: real API volumes kept as-is (including real
      zeros — a 0 in one direction must NOT be replaced by the other
      direction's value), only genuinely missing slots are forward-filled
    - highReal / lowReal / highVolReal / lowVolReal: masks telling the
      frontend where real API data exists (markers + tooltip "N min ago").
      Volume masks depend ONLY on the volume column: the wiki API reports
      volume 0 for a direction with no trades even when that side's price
      is null, and that real 0 must be displayed, not gap-filled.
    """
    conn = get_conn()
    cutoff: int | None = None
    if lookback_hours:
        cutoff = int(time.time()) - lookback_hours * 3600
    with _lock:
        if cutoff is None:
            rows = conn.execute(
                "SELECT timestamp, avg_high_price, avg_low_price, high_volume, low_volume "
                "FROM price_data WHERE item_id=? ORDER BY timestamp",
                (item_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp, avg_high_price, avg_low_price, high_volume, low_volume "
                "FROM price_data WHERE item_id=? AND timestamp >= ? ORDER BY timestamp",
                (item_id, cutoff),
            ).fetchall()

    timestamps: list[int] = []
    high: list = []
    low: list = []
    hv: list = []
    lv: list = []
    high_real: list[bool] = []
    low_real: list[bool] = []
    hv_real: list[bool] = []
    lv_real: list[bool] = []

    for r in rows:
        hp, lp, hvol, lvol = r["avg_high_price"], r["avg_low_price"], r["high_volume"], r["low_volume"]
        timestamps.append(r["timestamp"] * 1000)
        high.append(float(hp) if hp is not None else None)
        low.append(float(lp) if lp is not None else None)
        # A volume is real whenever the API reported it, INDEPENDENTLY of
        # whether the matching price is present.  The wiki API returns a
        # real 0 for a direction with no trades in that slot (e.g.
        # high_volume=1, low_volume=0), so a 0 must be kept and displayed
        # as 0 — never overwritten with the other direction's value or
        # gap-filled from a neighbor.
        hv.append(float(hvol) if hvol is not None else None)
        lv.append(float(lvol) if lvol is not None else None)
        high_real.append(hp is not None)
        low_real.append(lp is not None)
        hv_real.append(hvol is not None)
        lv_real.append(lvol is not None)

    return {
        "timestamps": timestamps,
        "highPrice": interp_gaps(high, timestamps),
        "lowPrice": interp_gaps(low, timestamps),
        "highVolume": fill_gaps(hv),
        "lowVolume": fill_gaps(lv),
        "highReal": high_real,
        "lowReal": low_real,
        "highVolReal": hv_real,
        "lowVolReal": lv_real,
    }


def stats_for(item_id: int, lookback_hours: int | None = None) -> dict:
    """Latest buy/sell price + total volume for *item_id*.

    The latest prices are the latest regardless of window; the volume sum
    (and point count) is restricted to *lookback_hours* when given so the
    "Vol" stat matches the currently viewed period.
    """
    conn = get_conn()
    cutoff: int | None = None
    if lookback_hours:
        cutoff = int(time.time()) - lookback_hours * 3600
    with _lock:
        if cutoff is None:
            row = conn.execute(
                """
                SELECT
                  (SELECT avg_high_price FROM price_data
                    WHERE item_id=? AND avg_high_price IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1) AS latest_buy,
                  (SELECT avg_low_price FROM price_data
                    WHERE item_id=? AND avg_low_price IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1) AS latest_sell,
                  (SELECT COALESCE(SUM(COALESCE(high_volume,0) + COALESCE(low_volume,0)), 0)
                    FROM price_data WHERE item_id=?) AS total_volume,
                  (SELECT COUNT(*) FROM price_data WHERE item_id=?) AS points
                """,
                (item_id, item_id, item_id, item_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                  (SELECT avg_high_price FROM price_data
                    WHERE item_id=? AND avg_high_price IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1) AS latest_buy,
                  (SELECT avg_low_price FROM price_data
                    WHERE item_id=? AND avg_low_price IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1) AS latest_sell,
                  (SELECT COALESCE(SUM(COALESCE(high_volume,0) + COALESCE(low_volume,0)), 0)
                    FROM price_data WHERE item_id=? AND timestamp >= ?) AS total_volume,
                  (SELECT COUNT(*) FROM price_data WHERE item_id=? AND timestamp >= ?) AS points
                """,
                (item_id, item_id, item_id, cutoff, item_id, cutoff),
            ).fetchone()
    return {
        "latest_buy": row["latest_buy"],
        "latest_sell": row["latest_sell"],
        "total_volume": row["total_volume"],
        "points": row["points"],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Meta
# ─────────────────────────────────────────────────────────────────────────────

def get_meta(key: str) -> str | None:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
