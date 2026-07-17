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
from app.esi.client import esi_client

from app.market.prices import (
    fetch_adjusted_prices,
    fetch_jita_price,
    fetch_jita_prices_bulk,
    fetch_region_orders_bulk,
    ensure_price_table,
    PRICE_CACHE_TTL,
    JITA_REGION,
)


def get_cached_jita_prices(conn: sqlite3.Connection, type_ids: list[int]) -> dict[int, tuple[float | None, float | None]]:
    """Vrátí všechny ceny z cache (poslední načtená Jita / The Forge sell).

    Cache se NEexpiruje — vždy se použije poslední fetched hodnota. Reálná
    cena je často víc reprezentativní než ESI 30-day average, navíc bulk
    refresh /markets/{region}/orders/ vrací nejnižší sell v celém regionu
    The Forge (Jita station + okolní systémy), takže pokud zrovna v Jitě
    neleží žádný sell order, použije se nejbližší v regionu.

    PRICE_CACHE_TTL se používá jen pro UI freshness indicator
    (`fresh` flag v /prices), ne pro filtrování hodnoty.
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

    Priorita: custom override > Jita / The Forge sell cache (poslední
    načtená, nikdy neexpiruje) > ESI markets/prices average_price (jen
    pro typy které ještě nebyly nikdy cachovány).
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


def get_all_price_items(
    conn: sqlite3.Connection,
    relevant_ids: set[int] | None = None,
) -> list[dict]:
    """Vrátí itemy z cache pro initial render.

    Pokud `relevant_ids` je předán, vrátí jen ty + všechny s custom_price.
    Bez něj vrátí celou cache (legacy chování — pomalé pro 19k+ řádků).

    Pro velkou cache (~19k typů) je render všech řádků v HTML extrémně pomalý
    (48 MB+ stránka). Místo toho UI loaduje zbytek přes `/api/prices/search` na
    vyžádání. Default sada = user assets + blueprints + custom_price overrides.
    """
    ensure_price_table(conn)
    if relevant_ids is None:
        where_clause = ""
        params: tuple = ()
    else:
        # Vždy zahrň všechno s custom_price
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


def _persist_bulk_orders(
    conn: sqlite3.Connection,
    bulk: dict[int, dict],
    wanted: set[int],
) -> tuple[int, list[int]]:
    """Zapíše agregovaná data z bulk fetch do market_price_cache.
    Pro type_ids ze `wanted` které nemají žádný order (chybí v `bulk`) zapíše None
    (žádná aktivní objednávka v regionu = explicitně bez ceny).
    Vrátí (počet refreshnutých záznamů, seznam type_ids s aspoň jedním orderem).
    """
    now = time.time()
    rows: list[tuple] = []
    refreshed = 0
    traded: list[int] = []
    for tid in wanted:
        d = bulk.get(tid)
        if d is None:
            # Žádný order v regionu → zapíšeme None (explicitně bez ceny)
            rows.append((tid, None, None, None, now))
            continue
        sell = d.get("sell")
        buy  = d.get("buy")
        jita_avail = d.get("jita_available")
        if sell is not None or buy is not None:
            refreshed += 1
            traded.append(tid)
        # Volume (7-day history) v tomto refresh nepřepisujeme — zachová stará hodnota
        rows.append((tid, sell, buy, jita_avail, now))
    # Použijeme COALESCE pro volume — INSERT OR REPLACE smaže existující volume,
    # takže místo toho použijeme UPSERT
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
    """Pro každý type_id stáhne 7-day Jita history a uloží volume.
    Paralelně přes _fetch_region_volume (semaphore 10 v market/prices.py).
    Vrátí počet úspěšně aktualizovaných řádků.

    progress_cb(done, total) volaný v rámci komítů.
    """
    from app.market.prices import _fetch_region_volume, JITA_REGION  # type: ignore

    if not type_ids:
        return 0

    done_holder = [0]
    total = len(type_ids)
    BATCH = 200       # commit po 200 výsledcích — drží otevřený DB write krátký

    async def _one(client: httpx.AsyncClient, tid: int) -> tuple[int, int | None]:
        vol = await _fetch_region_volume(client, JITA_REGION, tid)
        return tid, vol

    updated = 0
    async with esi_client() as client:
        # Zpracovávej v dávkách aby šel průběh hlásit a commitnout postupně.
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
    """Stáhne čerstvé Jita ceny pro všechny předané type_ids — bulk paginated region orders.
    Pak pro typy s aspoň jedním orderem dotáhne i 7-day volume z history endpointu.
    Vrátí počet typů s aspoň jednou cenou.
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
    se posílá po každé stránce orders endpointu (~500 stránek pro Jita region).
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


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "nikdy"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
