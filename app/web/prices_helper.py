"""
Pomocné funkce pro načítání cen ve web UI.

Strategie: Jita z cache pokud dostupné, jinak adjusted prices.
"""
from __future__ import annotations
import asyncio
import json as _json
import sqlite3
import time
import httpx

from app.market.prices import (
    fetch_adjusted_prices,
    fetch_jita_price,
    fetch_jita_prices_bulk,
    ensure_price_table,
    PRICE_CACHE_TTL,
)


def get_cached_jita_prices(conn: sqlite3.Connection, type_ids: list[int]) -> dict[int, tuple[float | None, float | None]]:
    """Vrátí jen ceny které jsou v cache a nejsou stale."""
    result = {}
    for tid in type_ids:
        row = conn.execute(
            "SELECT sell_price, buy_price, cached_at FROM market_price_cache WHERE type_id=?",
            (tid,)
        ).fetchone()
        if row and (time.time() - (row[2] or 0)) < PRICE_CACHE_TTL:
            result[tid] = (row[0], row[1])
    return result


def get_price_cache_stats(conn: sqlite3.Connection) -> dict:
    """Statistiky cache cen."""
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
    Vrátí ceny pro seznam type_ids.
    Priorita: custom override > Jita cache > adjusted price.
    """
    ensure_price_table(conn)
    jita = get_cached_jita_prices(conn, type_ids)

    missing = [tid for tid in type_ids if tid not in jita]
    adjusted: dict[int, tuple[float | None, float | None]] = {}

    if missing:
        async with httpx.AsyncClient() as client:
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


def get_all_price_items(conn: sqlite3.Connection) -> list[dict]:
    """Vrátí všechny itemy z cache s názvy, objemem a custom cenami, seřazené podle názvu."""
    ensure_price_table(conn)
    rows = conn.execute("""
        SELECT m.type_id, t.name, m.sell_price, m.buy_price, m.cached_at, c.price AS custom_price, m.volume, m.jita_available
        FROM market_price_cache m
        LEFT JOIN sde_types t ON t.type_id = m.type_id
        LEFT JOIN custom_price_override c ON c.type_id = m.type_id
        ORDER BY t.name ASC NULLS LAST
    """).fetchall()
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
    """Uloží nebo smaže custom cenu pro daný type_id."""
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


async def refresh_jita_prices_all(conn: sqlite3.Connection, type_ids: list[int]) -> int:
    """Stáhne čerstvé Jita ceny pro všechny předané type_ids. Vrátí počet úspěšně načtených."""
    ensure_price_table(conn)
    async with httpx.AsyncClient() as client:
        result = await fetch_jita_prices_bulk(client, conn, type_ids, force=True)
    return sum(1 for v in result.values() if v[0] is not None)


async def stream_jita_refresh(conn: sqlite3.Connection, type_ids: list[int]):
    """Async generator yielding SSE text chunks for Jita price refresh progress."""
    ensure_price_table(conn)
    total = len(type_ids)
    counter = [0]

    async def _fetch_one(client, tid):
        await fetch_jita_price(client, conn, tid, force=True)
        counter[0] += 1

    async def _run():
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                *[_fetch_one(client, tid) for tid in type_ids],
                return_exceptions=True,
            )

    task = asyncio.create_task(_run())

    while not task.done():
        pct = int(counter[0] * 100 / total) if total else 100
        yield f"data: {_json.dumps({'current': counter[0], 'total': total, 'pct': pct})}\n\n"
        await asyncio.sleep(0.4)

    await task
    yield f"data: {_json.dumps({'pct': 100, 'done': True, 'refreshed': counter[0], 'total': total})}\n\n"


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "nikdy"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
