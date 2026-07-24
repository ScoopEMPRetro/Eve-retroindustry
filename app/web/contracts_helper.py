"""
Veřejné kontrakty — per-region index do SQLite cache + lokální fulltext search.

Stáhne VŠECHNY veřejné kontrakty zvoleného regionu (metadata) i jejich položky
(1 volání/kontrakt), uloží do cache a pak se nad tím dá hledat cokoliv (podle
itemu, typu, ceny) bez dalších ESI volání. Viz diskuze: jediný způsob, jak
hledat podle itemu, protože výpis metadat položky neobsahuje a `title` bývá
prázdný.
"""
from __future__ import annotations
import asyncio
import json as _json
import sqlite3
import time

from app.character import contracts as contracts_api
from app.esi.client import esi_client


def ensure_public_contract_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS public_contract_meta (
            region_id      INTEGER PRIMARY KEY,
            indexed_at     REAL,
            contract_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS public_contracts (
            contract_id       INTEGER PRIMARY KEY,
            region_id         INTEGER,
            type              TEXT,
            price             REAL,
            reward            REAL,
            collateral        REAL,
            buyout            REAL,
            volume            REAL,
            date_expired      TEXT,
            title             TEXT,
            start_location_id INTEGER,
            end_location_id   INTEGER,
            issuer_id         INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pc_region ON public_contracts(region_id);
        CREATE TABLE IF NOT EXISTS public_contract_items (
            contract_id  INTEGER,
            type_id      INTEGER,
            quantity     INTEGER,
            is_included  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pci_contract ON public_contract_items(contract_id);
        CREATE INDEX IF NOT EXISTS idx_pci_type ON public_contract_items(type_id);
    """)
    conn.commit()


def get_index_status(conn: sqlite3.Connection, region_id: int) -> dict | None:
    ensure_public_contract_tables(conn)
    row = conn.execute(
        "SELECT indexed_at, contract_count FROM public_contract_meta WHERE region_id=?",
        (region_id,),
    ).fetchone()
    if not row:
        return None
    return {"indexed_at": row[0], "contract_count": row[1]}


def _store(conn: sqlite3.Connection, region_id: int, contracts: list[dict],
           items_by_cid: dict[int, list[dict]]) -> None:
    ensure_public_contract_tables(conn)
    cids = [c["contract_id"] for c in contracts if c.get("contract_id")]
    # smaž starý index regionu
    conn.execute("DELETE FROM public_contracts WHERE region_id=?", (region_id,))
    if cids:
        ph = ",".join("?" * len(cids))
        conn.execute(f"DELETE FROM public_contract_items WHERE contract_id IN ({ph})", cids)
    conn.executemany(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price, "
        "reward, collateral, buyout, volume, date_expired, title, start_location_id, "
        "end_location_id, issuer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c.get("contract_id"), region_id, c.get("type"), c.get("price") or 0,
          c.get("reward") or 0, c.get("collateral") or 0, c.get("buyout") or 0,
          c.get("volume") or 0, c.get("date_expired", ""), c.get("title") or "",
          c.get("start_location_id"), c.get("end_location_id"), c.get("issuer_id"))
         for c in contracts],
    )
    item_rows = []
    for cid, items in items_by_cid.items():
        for it in items:
            if it.get("type_id"):
                item_rows.append((cid, it["type_id"], it.get("quantity", 0),
                                  1 if it.get("is_included", True) else 0))
    if item_rows:
        conn.executemany(
            "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included) "
            "VALUES (?,?,?,?)", item_rows)
    conn.execute(
        "INSERT OR REPLACE INTO public_contract_meta (region_id, indexed_at, contract_count) "
        "VALUES (?,?,?)", (region_id, time.time(), len(contracts)))
    conn.commit()


async def stream_public_index(conn: sqlite3.Connection, region_id: int):
    """SSE generator: stáhne výpis (stránky) + položky (per kontrakt) a uloží."""
    ensure_public_contract_tables(conn)
    total_pages = [0]
    done_pages = [0]
    holder: dict = {}

    def _list_prog(done, total):
        done_pages[0] = done
        total_pages[0] = total

    async def _run_list():
        async with esi_client() as client:
            holder["list"] = await contracts_api.fetch_public_contracts(
                client, region_id, progress_cb=_list_prog)

    task = asyncio.create_task(_run_list())
    while not task.done():
        tp = total_pages[0]
        pct = int(done_pages[0] * 40 / tp) if tp else 0
        yield f"data: {_json.dumps({'phase':'list','done':done_pages[0],'total':tp,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task
    contracts = holder.get("list", [])

    # Položky jen pro typy s obsahem (courier/loan zpravidla položky nemají).
    item_contracts = [c for c in contracts if c.get("type") in ("item_exchange", "auction")]
    total_items = len(item_contracts)
    done_items = [0]
    items_by_cid: dict[int, list[dict]] = {}
    lock = asyncio.Lock()

    async def _one(client, c):
        its = await contracts_api.fetch_public_contract_items(client, c["contract_id"])
        async with lock:
            if its:
                items_by_cid[c["contract_id"]] = its
            done_items[0] += 1

    async def _run_items():
        async with esi_client() as client:
            await asyncio.gather(*[_one(client, c) for c in item_contracts],
                                 return_exceptions=True)

    yield f"data: {_json.dumps({'phase':'items','done':0,'total':total_items,'pct':40})}\n\n"
    task2 = asyncio.create_task(_run_items())
    while not task2.done():
        pct = 40 + (int(done_items[0] * 55 / total_items) if total_items else 55)
        yield f"data: {_json.dumps({'phase':'items','done':done_items[0],'total':total_items,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task2

    _store(conn, region_id, contracts, items_by_cid)
    yield f"data: {_json.dumps({'done':True,'pct':100,'contract_count':len(contracts)})}\n\n"


def search_public_contracts(conn: sqlite3.Connection, region_id: int, *,
                            item: str = "", ctype: str = "", max_price: float | None = None,
                            limit: int = 300) -> list[dict]:
    ensure_public_contract_tables(conn)
    where = ["c.region_id = ?"]
    params: list = [region_id]
    joins = ""
    if item.strip():
        joins = (" JOIN public_contract_items i ON i.contract_id = c.contract_id"
                 " JOIN sde_types t ON t.type_id = i.type_id")
        where.append("t.name LIKE ?")
        params.append(f"%{item.strip()}%")
    if ctype:
        where.append("c.type = ?")
        params.append(ctype)
    if max_price is not None:
        where.append("c.price <= ?")
        params.append(max_price)
    sql = (f"SELECT DISTINCT c.contract_id, c.type, c.price, c.reward, c.collateral, "
           f"c.volume, c.date_expired, c.title, c.start_location_id, c.end_location_id, "
           f"c.issuer_id FROM public_contracts c{joins} WHERE {' AND '.join(where)} "
           f"ORDER BY c.price LIMIT ?")
    params.append(limit)
    cols = ["contract_id", "type", "price", "reward", "collateral", "volume",
            "date_expired", "title", "start_location_id", "end_location_id", "issuer_id"]
    return [dict(zip(cols, row)) for row in conn.execute(sql, params).fetchall()]


def get_contract_items(conn: sqlite3.Connection, contract_id: int) -> list[dict]:
    ensure_public_contract_tables(conn)
    rows = conn.execute(
        "SELECT i.type_id, i.quantity, i.is_included, COALESCE(t.name, '#'||i.type_id) "
        "FROM public_contract_items i LEFT JOIN sde_types t ON t.type_id = i.type_id "
        "WHERE i.contract_id=?", (contract_id,)).fetchall()
    return [{"type_id": r[0], "quantity": r[1], "included": bool(r[2]), "name": r[3]}
            for r in rows]
