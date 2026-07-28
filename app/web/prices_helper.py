"""
Helper functions for loading prices in the web UI.

Strategy: Jita from cache if available, otherwise adjusted prices.
"""
from __future__ import annotations
import asyncio
import json as _json
import sqlite3
import time
import httpx
from app.esi.client import esi_client

from app.market.prices import (
    fetch_adjusted_prices,
    fetch_jita_price,
    fetch_jita_prices_bulk,
    fetch_region_orders_bulk,
    ensure_price_table,
    PRICE_CACHE_TTL,
    JITA_REGION,
    TRADE_HUBS,
)


def get_cached_jita_prices(conn: sqlite3.Connection, type_ids: list[int]) -> dict[int, tuple[float | None, float | None]]:
    """Returns all prices from the cache (last fetched Jita / The Forge sell).

    The cache does NOT expire — the last fetched value is always used. The real
    price is often more representative than the ESI 30-day average, and moreover a bulk
    refresh of /markets/{region}/orders/ returns the lowest sell in the entire
    The Forge region (Jita station + surrounding systems), so if there is currently no
    sell order in Jita itself, the nearest one in the region is used.

    PRICE_CACHE_TTL is used only for the UI freshness indicator
    (`fresh` flag in /prices), not for filtering the value.
    """
    result = {}
    for tid in type_ids:
        row = conn.execute(
            "SELECT sell_price, buy_price FROM market_price_cache WHERE type_id=?",
            (tid,)
        ).fetchone()
        if row and (row[0] is not None or row[1] is not None):
            result[tid] = (row[0], row[1])
    return result


def get_price_cache_stats(conn: sqlite3.Connection) -> dict:
    """Price cache statistics."""
    row = conn.execute(
        "SELECT COUNT(*), MAX(cached_at), MIN(cached_at) FROM market_price_cache WHERE sell_price IS NOT NULL"
    ).fetchone()
    count = row[0] or 0
    last_update = row[1]
    fresh = 0
    stale = 0
    if count > 0:
        now = time.time()
        r2 = conn.execute("SELECT cached_at FROM market_price_cache").fetchall()
        for (ts,) in r2:
            if ts and (now - ts) < PRICE_CACHE_TTL:
                fresh += 1
            else:
                stale += 1
    return {
        "total": count,
        "fresh": fresh,
        "stale": stale,
        "last_update": last_update,
        "last_update_str": _fmt_ts(last_update),
    }


def _load_custom_overrides(conn: sqlite3.Connection, type_ids: list[int]) -> dict[int, float]:
    if not type_ids:
        return {}
    placeholders = ",".join("?" * len(type_ids))
    rows = conn.execute(
        f"SELECT type_id, price FROM custom_price_override WHERE type_id IN ({placeholders})",
        type_ids,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


async def get_prices_for_ids(
    conn: sqlite3.Connection,
    type_ids: list[int],
) -> dict[int, tuple[float | None, float | None]]:
    """
    Returns prices for a list of type_ids.

    Priority: custom override > Jita / The Forge sell cache (last
    fetched, never expires) > ESI markets/prices average_price (only
    for types that have never been cached yet).
    """
    ensure_price_table(conn)
    jita = get_cached_jita_prices(conn, type_ids)

    missing = [tid for tid in type_ids if tid not in jita]
    adjusted: dict[int, tuple[float | None, float | None]] = {}

    if missing:
        async with esi_client() as client:
            adj_raw = await fetch_adjusted_prices(client)
        for tid in missing:
            entry = adj_raw.get(tid, {})
            avg = entry.get("average_price")
            adjusted[tid] = (avg, None)

    result = {**adjusted, **jita}

    custom = _load_custom_overrides(conn, type_ids)
    for tid, price in custom.items():
        buy = result.get(tid, (None, None))[1]
        result[tid] = (price, buy)

    return result


def get_cached_prices_for_ids(
    conn: sqlite3.Connection,
    type_ids: list[int],
) -> dict[int, tuple[float | None, float | None]]:
    """Like :func:`get_prices_for_ids` but NEVER touches ESI — cached Jita/Forge
    sell prices plus custom overrides only. Types with no cached price simply get
    no entry. Synchronous and instant, so the dashboard can render immediately;
    the ``/api/dashboard/live`` endpoint fills in ESI-derived values afterwards."""
    ensure_price_table(conn)
    result: dict[int, tuple[float | None, float | None]] = dict(
        get_cached_jita_prices(conn, type_ids)
    )
    custom = _load_custom_overrides(conn, type_ids)
    for tid, price in custom.items():
        buy = result.get(tid, (None, None))[1]
        result[tid] = (price, buy)
    return result


def get_all_price_items(
    conn: sqlite3.Connection,
    relevant_ids: set[int] | None = None,
) -> list[dict]:
    """Returns items from the cache for the initial render.

    If `relevant_ids` is passed, returns only those + everything with a custom_price.
    Without it, returns the entire cache (legacy behavior — slow for 19k+ rows).

    For a large cache (~19k types), rendering all rows in HTML is extremely slow
    (48 MB+ page). Instead, the UI loads the rest via `/api/prices/search` on
    demand. Default set = user assets + blueprints + custom_price overrides.
    """
    ensure_price_table(conn)
    if relevant_ids is None:
        where_clause = ""
        params: tuple = ()
    else:
        # Always include everything with a custom_price
        ph = ",".join("?" * len(relevant_ids)) if relevant_ids else "NULL"
        where_clause = (
            f"WHERE m.type_id IN ({ph}) OR c.price IS NOT NULL"
            if relevant_ids
            else "WHERE c.price IS NOT NULL"
        )
        params = tuple(relevant_ids)

    rows = conn.execute(f"""
        SELECT m.type_id, t.name, m.sell_price, m.buy_price, m.cached_at,
               c.price AS custom_price, m.volume, m.jita_available
        FROM market_price_cache m
        LEFT JOIN sde_types t ON t.type_id = m.type_id
        LEFT JOIN custom_price_override c ON c.type_id = m.type_id
        {where_clause}
        ORDER BY t.name ASC NULLS LAST
    """, params).fetchall()
    now = time.time()
    return [
        {
            "type_id": r[0],
            "name": r[1] or f"Unknown #{r[0]}",
            "sell_price": r[2],
            "buy_price": r[3],
            "fresh": bool(r[4] and (now - r[4]) < PRICE_CACHE_TTL),
            "custom_price": r[5],
            "volume": r[6],
            "jita_available": r[7],
        }
        for r in rows
    ]


def set_custom_price(conn: sqlite3.Connection, type_id: int, price: float | None):
    """Stores or deletes the custom price for the given type_id."""
    ensure_price_table(conn)
    if price is None:
        conn.execute("DELETE FROM custom_price_override WHERE type_id=?", (type_id,))
    else:
        conn.execute(
            "INSERT INTO custom_price_override (type_id, price, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(type_id) DO UPDATE SET price=excluded.price, updated_at=excluded.updated_at",
            (type_id, price, time.time()),
        )
    conn.commit()


def _persist_bulk_orders(
    conn: sqlite3.Connection,
    bulk: dict[int, dict],
    wanted: set[int],
) -> tuple[int, list[int]]:
    """Writes aggregated data from the bulk fetch into market_price_cache.
    For type_ids from `wanted` that have no order (missing in `bulk`) it writes None
    (no active order in the region = explicitly no price).
    Returns (number of refreshed records, list of type_ids with at least one order).
    """
    now = time.time()
    rows: list[tuple] = []
    refreshed = 0
    traded: list[int] = []
    for tid in wanted:
        d = bulk.get(tid)
        if d is None:
            # No order in the region → write None (explicitly no price)
            rows.append((tid, None, None, None, now))
            continue
        sell = d.get("sell")
        buy  = d.get("buy")
        jita_avail = d.get("available")
        if sell is not None or buy is not None:
            refreshed += 1
            traded.append(tid)
        # Volume (7-day history) is not overwritten in this refresh — the old value is kept
        rows.append((tid, sell, buy, jita_avail, now))
    # Use COALESCE for volume — INSERT OR REPLACE would erase the existing volume,
    # so use an UPSERT instead
    conn.executemany(
        """INSERT INTO market_price_cache (type_id, sell_price, buy_price, jita_available, cached_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(type_id) DO UPDATE SET
             sell_price = excluded.sell_price,
             buy_price = excluded.buy_price,
             jita_available = excluded.jita_available,
             cached_at = excluded.cached_at""",
        rows,
    )
    conn.commit()
    return refreshed, traded


async def _fill_volumes(
    conn: sqlite3.Connection,
    type_ids: list[int],
    progress_cb=None,
) -> int:
    """For each type_id, fetches the 7-day Jita history and stores the volume.
    In parallel via _fetch_region_volume (semaphore 10 in market/prices.py).
    Returns the number of successfully updated rows.

    progress_cb(done, total) called within commits.
    """
    from app.market.prices import _fetch_region_volume, JITA_REGION  # type: ignore

    if not type_ids:
        return 0

    done_holder = [0]
    total = len(type_ids)
    BATCH = 200       # commit every 200 results — keeps the open DB write short

    async def _one(client: httpx.AsyncClient, tid: int) -> tuple[int, int | None]:
        vol = await _fetch_region_volume(client, JITA_REGION, tid)
        return tid, vol

    updated = 0
    async with esi_client() as client:
        # Process in batches so progress can be reported and committed incrementally.
        for start in range(0, total, BATCH):
            batch = type_ids[start:start + BATCH]
            results = await asyncio.gather(
                *[_one(client, tid) for tid in batch], return_exceptions=True
            )
            rows = [(vol, tid) for r in results if not isinstance(r, Exception)
                    for tid, vol in [r] if vol is not None]
            if rows:
                conn.executemany(
                    "UPDATE market_price_cache SET volume=? WHERE type_id=?", rows
                )
                conn.commit()
                updated += len(rows)
            done_holder[0] = start + len(batch)
            if progress_cb:
                await _maybe_call(progress_cb, done_holder[0], total)
    return updated


async def _maybe_call(cb, *args):
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


async def refresh_jita_prices_all(conn: sqlite3.Connection, type_ids: list[int]) -> int:
    """Fetches fresh Jita prices for all passed type_ids — bulk paginated region orders.
    Then, for types with at least one order, also fetches the 7-day volume from the history endpoint.
    Returns the number of types with at least one price.
    """
    ensure_price_table(conn)
    wanted = set(type_ids)
    async with esi_client() as client:
        bulk = await fetch_region_orders_bulk(client, JITA_REGION)
    refreshed, traded = _persist_bulk_orders(conn, bulk, wanted)
    if traded:
        await _fill_volumes(conn, traded)
    return refreshed


async def stream_jita_refresh(conn: sqlite3.Connection, type_ids: list[int]):
    """Async generator yielding SSE chunks. Bulk paginated fetch — progress
    is sent after each page of the orders endpoint (~500 pages for the Jita region).
    """
    ensure_price_table(conn)
    wanted = set(type_ids)
    total_pages_holder = [0]
    completed_holder = [0]

    async def _progress(done: int, total: int):
        total_pages_holder[0] = total
        completed_holder[0] = done

    bulk_holder: dict = {}

    async def _run():
        async with esi_client() as client:
            bulk_holder.update(
                await fetch_region_orders_bulk(client, JITA_REGION, progress_cb=_progress)
            )

    task = asyncio.create_task(_run())
    while not task.done():
        total = total_pages_holder[0]
        done = completed_holder[0]
        # Phase 1 = order fetch — display 0–80 % so phase 2 (volumes)
        # has the last 20 %.
        pct = int(done * 80 / total) if total else 0
        yield f"data: {_json.dumps({'current': done, 'total': total, 'pct': pct, 'phase': 'orders'})}\n\n"
        await asyncio.sleep(0.5)
    await task

    refreshed, traded = _persist_bulk_orders(conn, bulk_holder, wanted)

    # Phase 2 — 7-day Jita volumes for everything that actually trades.
    vol_done_holder = [0]
    vol_total = len(traded)
    yield f"data: {_json.dumps({'pct': 80, 'phase': 'volumes', 'vol_done': 0, 'vol_total': vol_total})}\n\n"

    async def _vol_progress(done: int, total: int):
        vol_done_holder[0] = done

    vol_task = asyncio.create_task(_fill_volumes(conn, traded, progress_cb=_vol_progress))
    while not vol_task.done():
        d = vol_done_holder[0]
        pct = 80 + int(d * 20 / vol_total) if vol_total else 100
        yield f"data: {_json.dumps({'phase': 'volumes', 'vol_done': d, 'vol_total': vol_total, 'pct': pct})}\n\n"
        await asyncio.sleep(0.5)
    updated_vol = await vol_task

    yield f"data: {_json.dumps({'pct': 100, 'done': True, 'refreshed': refreshed, 'total': len(wanted), 'volume_updated': updated_vol})}\n\n"


# ---------------------------------------------------------------------------
# Secondary trade hubs (Amarr / Dodixie / Rens / Hek) — same pipeline as Jita,
# fetched on demand per hub, stored in hub_price_cache keyed by region_id.
# ---------------------------------------------------------------------------

def _persist_hub_bulk_orders(
    conn: sqlite3.Connection,
    region_id: int,
    bulk: dict[int, dict],
    wanted: set[int],
) -> tuple[int, list[int]]:
    """Write region-wide best sell/buy into hub_price_cache. Mirrors
    _persist_bulk_orders but keyed by (region_id, type_id) and keeps any existing
    volume (filled separately). Returns (refreshed_count, traded_type_ids)."""
    now = time.time()
    rows: list[tuple] = []
    refreshed = 0
    traded: list[int] = []
    for tid in wanted:
        d = bulk.get(tid)
        if d is None:
            rows.append((region_id, tid, None, None, None, now))
            continue
        sell = d.get("sell")
        buy = d.get("buy")
        avail = d.get("available")
        if sell is not None or buy is not None:
            refreshed += 1
            traded.append(tid)
        rows.append((region_id, tid, sell, buy, avail, now))
    # volume is filled separately (7-day history) — don't overwrite it here.
    conn.executemany(
        """INSERT INTO hub_price_cache (region_id, type_id, sell_price, buy_price, available, cached_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(region_id, type_id) DO UPDATE SET
             sell_price = excluded.sell_price,
             buy_price = excluded.buy_price,
             available = excluded.available,
             cached_at = excluded.cached_at""",
        rows,
    )
    conn.commit()
    return refreshed, traded


async def _fill_hub_volumes(
    conn: sqlite3.Connection,
    region_id: int,
    type_ids: list[int],
    progress_cb=None,
) -> int:
    """7-day region volume for a hub, stored in hub_price_cache. Mirrors
    _fill_volumes but for an arbitrary region."""
    from app.market.prices import _fetch_region_volume  # type: ignore
    if not type_ids:
        return 0
    done_holder = [0]
    total = len(type_ids)
    BATCH = 200

    async def _one(client: httpx.AsyncClient, tid: int) -> tuple[int, int | None]:
        vol = await _fetch_region_volume(client, region_id, tid)
        return tid, vol

    updated = 0
    async with esi_client() as client:
        for start in range(0, total, BATCH):
            batch = type_ids[start:start + BATCH]
            results = await asyncio.gather(
                *[_one(client, tid) for tid in batch], return_exceptions=True
            )
            rows = [(vol, region_id, tid) for r in results if not isinstance(r, Exception)
                    for tid, vol in [r] if vol is not None]
            if rows:
                conn.executemany(
                    "UPDATE hub_price_cache SET volume=? WHERE region_id=? AND type_id=?", rows
                )
                conn.commit()
                updated += len(rows)
            done_holder[0] = start + len(batch)
            if progress_cb:
                await _maybe_call(progress_cb, done_holder[0], total)
    return updated


async def stream_hub_refresh(conn: sqlite3.Connection, type_ids: list[int], region_id: int):
    """SSE generator for a single trade hub. Same two phases as Jita
    (region orders → 7-day volumes), writing to hub_price_cache."""
    ensure_price_table(conn)
    wanted = set(type_ids)
    total_pages_holder = [0]
    completed_holder = [0]

    async def _progress(done: int, total: int):
        total_pages_holder[0] = total
        completed_holder[0] = done

    bulk_holder: dict = {}

    station = TRADE_HUBS.get(region_id, {}).get("station", 0)

    async def _run():
        async with esi_client() as client:
            bulk_holder.update(
                await fetch_region_orders_bulk(client, region_id, progress_cb=_progress, station_id=station)
            )

    task = asyncio.create_task(_run())
    while not task.done():
        total = total_pages_holder[0]
        done = completed_holder[0]
        pct = int(done * 80 / total) if total else 0
        yield f"data: {_json.dumps({'current': done, 'total': total, 'pct': pct, 'phase': 'orders'})}\n\n"
        await asyncio.sleep(0.5)
    await task

    refreshed, traded = _persist_hub_bulk_orders(conn, region_id, bulk_holder, wanted)

    vol_done_holder = [0]
    vol_total = len(traded)
    yield f"data: {_json.dumps({'pct': 80, 'phase': 'volumes', 'vol_done': 0, 'vol_total': vol_total})}\n\n"

    async def _vol_progress(done: int, total: int):
        vol_done_holder[0] = done

    vol_task = asyncio.create_task(_fill_hub_volumes(conn, region_id, traded, progress_cb=_vol_progress))
    while not vol_task.done():
        d = vol_done_holder[0]
        pct = 80 + int(d * 20 / vol_total) if vol_total else 100
        yield f"data: {_json.dumps({'phase': 'volumes', 'vol_done': d, 'vol_total': vol_total, 'pct': pct})}\n\n"
        await asyncio.sleep(0.5)
    updated_vol = await vol_task

    yield f"data: {_json.dumps({'pct': 100, 'done': True, 'refreshed': refreshed, 'total': len(wanted), 'volume_updated': updated_vol})}\n\n"


def get_hub_cache_stats(conn: sqlite3.Connection, region_id: int) -> dict:
    """Row count + last-update timestamp for one hub's cache."""
    row = conn.execute(
        "SELECT COUNT(*), MAX(cached_at) FROM hub_price_cache "
        "WHERE region_id=? AND sell_price IS NOT NULL",
        (region_id,),
    ).fetchone()
    count = row[0] or 0
    last_update = row[1]
    return {
        "total": count,
        "has_data": count > 0,
        "last_update": last_update,
        "last_update_str": _fmt_ts(last_update),
    }


def get_all_hub_prices(
    conn: sqlite3.Connection,
    type_ids: list[int],
) -> dict[int, dict[int, dict]]:
    """For the given type_ids, return {type_id: {region_id: {sell, buy, volume}}}
    across every cached hub — used to attach comparison columns to price rows."""
    if not type_ids:
        return {}
    out: dict[int, dict[int, dict]] = {}
    ph = ",".join("?" * len(type_ids))
    rows = conn.execute(
        f"SELECT type_id, region_id, sell_price, buy_price, volume, available "
        f"FROM hub_price_cache WHERE type_id IN ({ph})",
        list(type_ids),
    ).fetchall()
    for tid, rid, sell, buy, vol, avail in rows:
        if sell is None and buy is None and vol is None and avail is None:
            continue
        out.setdefault(tid, {})[rid] = {"sell": sell, "buy": buy, "volume": vol, "available": avail}
    return out


HISTORY_TTL = 60 * 60 * 8  # 8 h — market history updates about once a day


def _densify_history(series: list[dict]) -> list[dict]:
    """ESI market history omits days with no trades. For the chart we want a true
    daily timeline, so fill each missing calendar day between the first and last
    entry with volume 0 and the last known price carried forward. Without this an
    illiquid item (e.g. a 3B ISK SKIN that sells once a week) looks like it traded
    every day, and the range selector counts trades instead of days."""
    import datetime
    if not series:
        return series
    def pd(s):
        y, m, d = (int(x) for x in s.split("-"))
        return datetime.date(y, m, d)
    valid = [e for e in series if e.get("d")]
    if not valid:
        return series
    valid.sort(key=lambda e: e["d"])
    by_date = {e["d"]: e for e in valid}
    out: list[dict] = []
    last = valid[0]
    cur, end = pd(valid[0]["d"]), pd(valid[-1]["d"])
    step = datetime.timedelta(days=1)
    while cur <= end:
        key = cur.isoformat()
        e = by_date.get(key)
        if e is not None:
            out.append(e); last = e
        else:
            avg = last.get("avg")
            out.append({"d": key, "avg": avg, "low": avg, "high": avg, "vol": 0})
        cur += step
    return out


async def get_price_history(conn: sqlite3.Connection, region_id: int, type_id: int) -> list[dict]:
    """Daily market history (~1 year) for the price chart, cached per (region, type).
    Falls back to any stale cached copy if a fresh fetch fails."""
    ensure_price_table(conn)
    row = conn.execute(
        "SELECT data_json, cached_at FROM price_history_cache WHERE region_id=? AND type_id=?",
        (region_id, type_id),
    ).fetchone()
    # Densify on every return (incl. cache hits) so no-trade days are filled even
    # for cache rows written before densifying existed. It's idempotent.
    if row and (time.time() - (row[1] or 0)) < HISTORY_TTL:
        try:
            return _densify_history(_json.loads(row[0]))
        except Exception:
            pass

    from app.market.prices import fetch_region_history
    async with esi_client() as client:
        series = await fetch_region_history(client, region_id, type_id)

    if series is None:  # fetch failed — serve stale if we have it
        if row:
            try:
                return _densify_history(_json.loads(row[0]))
            except Exception:
                return []
        return []

    conn.execute(
        """INSERT INTO price_history_cache (region_id, type_id, data_json, cached_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(region_id, type_id) DO UPDATE SET
             data_json = excluded.data_json, cached_at = excluded.cached_at""",
        (region_id, type_id, _json.dumps(series), time.time()),
    )
    conn.commit()
    return _densify_history(series)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "never"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
