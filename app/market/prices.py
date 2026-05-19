"""
Načítání tržních cen z ESI.

Dva režimy:
  adjusted  – globální adjusted/average prices, jeden API call
  jita      – živé Jita sell/buy ceny, N parallel calls, cache 30 min
"""
import asyncio
import time
import sqlite3
import httpx

ESI_BASE = "https://esi.evetech.net/latest"
JITA_REGION = 10000002   # The Forge
JITA_STATION = 60003760  # Jita 4-4 CNAP
PRICE_CACHE_TTL = 60 * 30  # 30 minut

_JITA_SEM = asyncio.Semaphore(20)


# ---------------------------------------------------------------------------
# DB schéma
# ---------------------------------------------------------------------------

def ensure_price_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_price_cache (
            type_id    INTEGER PRIMARY KEY,
            sell_price REAL,
            buy_price  REAL,
            cached_at  REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_price_override (
            type_id    INTEGER PRIMARY KEY,
            price      REAL NOT NULL,
            updated_at REAL
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_price_cache)")}
    if "volume" not in cols:
        conn.execute("ALTER TABLE market_price_cache ADD COLUMN volume INTEGER")
    if "jita_available" not in cols:
        conn.execute("ALTER TABLE market_price_cache ADD COLUMN jita_available INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_volume_cache (
            location_id    INTEGER NOT NULL,
            type_id        INTEGER NOT NULL,
            volume         INTEGER,
            best_sell      REAL,
            traded_volume  INTEGER,
            cached_at      REAL,
            PRIMARY KEY (location_id, type_id)
        )
    """)
    sv_cols = {r[1] for r in conn.execute("PRAGMA table_info(station_volume_cache)")}
    if "traded_volume" not in sv_cols:
        conn.execute("ALTER TABLE station_volume_cache ADD COLUMN traded_volume INTEGER")
    conn.commit()


# ---------------------------------------------------------------------------
# Adjusted prices (globální, 1 call)
# ---------------------------------------------------------------------------

async def fetch_adjusted_prices(client: httpx.AsyncClient) -> dict[int, dict]:
    """
    Vrátí {type_id: {adjusted_price, average_price}} pro všechny typy.
    Jeden API call — vhodné pro rychlý odhad.
    """
    r = await client.get(
        f"{ESI_BASE}/markets/prices/",
        params={"datasource": "tranquility"},
        timeout=20,
    )
    r.raise_for_status()
    return {d["type_id"]: d for d in r.json()}


# ---------------------------------------------------------------------------
# Jita live prices (per type, cached)
# ---------------------------------------------------------------------------

def _get_cached_price(conn: sqlite3.Connection, type_id: int) -> tuple[float | None, float | None]:
    row = conn.execute(
        "SELECT sell_price, buy_price, cached_at FROM market_price_cache WHERE type_id=?",
        (type_id,)
    ).fetchone()
    if row and (time.time() - (row[2] or 0)) < PRICE_CACHE_TTL:
        return row[0], row[1]
    return None, None


def _save_cached_price(
    conn: sqlite3.Connection,
    type_id: int,
    sell: float | None,
    buy: float | None,
    volume: int | None = None,
    jita_available: int | None = None,
):
    conn.execute(
        "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, volume, jita_available, cached_at) VALUES (?,?,?,?,?,?)",
        (type_id, sell, buy, volume, jita_available, time.time())
    )
    conn.commit()


async def _fetch_region_volume(client: httpx.AsyncClient, region_id: int, type_id: int) -> int | None:
    """Vrátí součet objemu za posledních 7 dní z ESI history pro daný region."""
    try:
        r = await client.get(
            f"{ESI_BASE}/markets/{region_id}/history/",
            params={"type_id": type_id, "datasource": "tranquility"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        history = r.json()
        if history:
            return sum(entry.get("volume", 0) for entry in history[-7:])
    except Exception:
        pass
    return None


async def _fetch_jita_volume(client: httpx.AsyncClient, type_id: int) -> int | None:
    return await _fetch_region_volume(client, JITA_REGION, type_id)


async def fetch_jita_price(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_id: int,
    force: bool = False,
) -> tuple[float | None, float | None]:
    """
    Vrátí (best_sell, best_buy) pro daný typ v Jitě.
    Používá cache — platná 30 minut. force=True přeskočí cache a vždy stáhne čerstvá data.
    """
    if not force:
        sell_c, buy_c = _get_cached_price(conn, type_id)
        if sell_c is not None or buy_c is not None:
            return sell_c, buy_c

    async with _JITA_SEM:
        orders_req = client.get(
            f"{ESI_BASE}/markets/{JITA_REGION}/orders/",
            params={"type_id": type_id, "order_type": "all", "datasource": "tranquility"},
            timeout=15,
        )
        volume_req = _fetch_jita_volume(client, type_id)
        orders_resp, volume = await asyncio.gather(orders_req, volume_req)

    if orders_resp.status_code == 404:
        _save_cached_price(conn, type_id, None, None, None, None)
        return None, None
    orders_resp.raise_for_status()

    orders = orders_resp.json()
    sell_orders = [o for o in orders if not o["is_buy_order"]]
    buy_orders  = [o for o in orders if o["is_buy_order"]]

    best_sell = min((o["price"] for o in sell_orders), default=None)
    best_buy  = max((o["price"] for o in buy_orders),  default=None)

    jita_available = sum(
        o.get("volume_remain", 0) for o in sell_orders
        if o.get("location_id") == JITA_STATION
    )

    _save_cached_price(conn, type_id, best_sell, best_buy, volume, jita_available)
    return best_sell, best_buy


async def fetch_jita_prices_bulk(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_ids: list[int],
    force: bool = False,
) -> dict[int, tuple[float | None, float | None]]:
    """Načte Jita ceny pro seznam typů paralelně."""
    tasks = [fetch_jita_price(client, conn, tid, force=force) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res if isinstance(res, tuple) else (None, None)
        for tid, res in zip(type_ids, results)
    }


_STATION_SEM = asyncio.Semaphore(20)
STATION_VOLUME_TTL = 60 * 30
_region_cache: dict[int, int] = {}  # structure_id → region_id (in-memory)


async def get_region_for_structure(structure_id: int) -> int | None:
    """Zjistí region_id pro strukturu přes ESI (system→constellation→region). Cachuje v paměti."""
    if structure_id in _region_cache:
        return _region_cache[structure_id]
    try:
        async with httpx.AsyncClient() as client:
            # NPC stanice: /universe/stations/{id}/ → system_id
            if structure_id < 1_000_000_000_000:
                r = await client.get(f"{ESI_BASE}/universe/stations/{structure_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
                sys_id = r.json().get("system_id") if r.status_code == 200 else None
            else:
                # Player struktura — nemáme token zde, zkusíme přes DB location_name_cache
                return None

            if not sys_id:
                return None

            sys_r = await client.get(f"{ESI_BASE}/universe/systems/{sys_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if sys_r.status_code != 200:
                return None
            con_id = sys_r.json().get("constellation_id")

            con_r = await client.get(f"{ESI_BASE}/universe/constellations/{con_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if con_r.status_code != 200:
                return None
            region_id = con_r.json().get("region_id")

        if region_id:
            _region_cache[structure_id] = region_id
        return region_id
    except Exception:
        return None


async def fetch_structure_market(
    conn: sqlite3.Connection,
    structure_id: int,
    token: str,
    our_type_ids: set[int],
    region_id: int | None = None,
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """
    Stáhne všechny sell objednávky z player struktury přes autorizovaný endpoint.
    Vrátí {type_id: (volume, best_sell)} jen pro type_ids z naší cache.
    Vyžaduje scope esi-markets.structure_markets.v1.
    """
    ensure_price_table(conn)
    aggregated: dict[int, dict] = {}
    page = 1

    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/structures/{structure_id}/",
                    params={"datasource": "tranquility", "page": page},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                )
            except Exception:
                break

            if r.status_code == 403:
                raise PermissionError("Nedostatečná oprávnění pro přístup k marketu struktury (403).")
            if r.status_code != 200:
                break

            orders = r.json()
            if not orders:
                break

            for o in orders:
                if o.get("is_buy_order"):
                    continue
                tid = o.get("type_id")
                if tid not in our_type_ids:
                    continue
                if tid not in aggregated:
                    aggregated[tid] = {"volume": 0, "best_sell": None}
                aggregated[tid]["volume"] += o.get("volume_remain", 0)
                price = o.get("price")
                if price and (aggregated[tid]["best_sell"] is None or price < aggregated[tid]["best_sell"]):
                    aggregated[tid]["best_sell"] = price

            total_pages = int(r.headers.get("X-Pages", 1))
            if page >= total_pages:
                break
            page += 1

    # Fetch history pro typy které mají alespoň nějaké objednávky
    traded_with_orders = {tid for tid, e in aggregated.items() if e["volume"] > 0}
    if region_id is None:
        region_id = await get_region_for_structure(structure_id)

    history_map: dict[int, int | None] = {}
    if region_id and traded_with_orders:
        async with httpx.AsyncClient() as client:
            hist_tasks = {tid: _fetch_region_volume(client, region_id, tid) for tid in traded_with_orders}
            hist_results = await asyncio.gather(*hist_tasks.values(), return_exceptions=True)
        for tid, res in zip(hist_tasks.keys(), hist_results):
            history_map[tid] = res if isinstance(res, int) else None

    now = time.time()
    result: dict[int, tuple[int | None, float | None, int | None]] = {}
    rows = []
    for tid in our_type_ids:
        entry = aggregated.get(tid)
        vol = entry["volume"] if entry else 0
        sell = entry["best_sell"] if entry else None
        traded = history_map.get(tid)
        result[tid] = (vol, sell, traded)
        rows.append((structure_id, tid, vol, sell, traded, now))

    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume, best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return result


async def _fetch_orders_for_type(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    type_id: int,
) -> tuple[int | None, float | None]:
    """Vrátí (volume_sum, best_sell) pro daný typ na konkrétní stanici."""
    async with _STATION_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/orders/",
                params={"type_id": type_id, "order_type": "sell", "datasource": "tranquility"},
                timeout=15,
            )
            if r.status_code != 200:
                return None, None
        except Exception:
            return None, None

    orders = [o for o in r.json() if o.get("location_id") == location_id]
    if not orders:
        return 0, None
    volume = sum(o.get("volume_remain", 0) for o in orders)
    best_sell = min(o["price"] for o in orders)
    return volume, best_sell


async def fetch_station_volumes(
    conn: sqlite3.Connection,
    location_id: int,
    region_id: int,
    type_ids: list[int],
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """Stáhne a uloží objemy+ceny+historii pro všechny type_ids na dané NPC stanici."""
    ensure_price_table(conn)
    async with httpx.AsyncClient() as client:
        order_tasks = [_fetch_orders_for_type(client, region_id, location_id, tid) for tid in type_ids]
        order_results = await asyncio.gather(*order_tasks, return_exceptions=True)

    # History jen pro typy s objednávkami
    order_map: dict[int, tuple] = {}
    for tid, res in zip(type_ids, order_results):
        order_map[tid] = res if isinstance(res, tuple) else (None, None)

    types_with_orders = [tid for tid, (vol, _) in order_map.items() if vol and vol > 0]
    history_map: dict[int, int | None] = {}
    if types_with_orders:
        async with httpx.AsyncClient() as client:
            hist_tasks = [_fetch_region_volume(client, region_id, tid) for tid in types_with_orders]
            hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
        for tid, res in zip(types_with_orders, hist_results):
            history_map[tid] = res if isinstance(res, int) else None

    now = time.time()
    rows = []
    result_map: dict[int, tuple[int | None, float | None, int | None]] = {}
    for tid in type_ids:
        vol, sell = order_map.get(tid, (None, None))
        traded = history_map.get(tid)
        rows.append((location_id, tid, vol, sell, traded, now))
        result_map[tid] = (vol, sell, traded)

    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume, best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return result_map


def get_cached_station_volumes(
    conn: sqlite3.Connection,
    location_id: int,
) -> dict[int, tuple[int | None, float | None, int | None]] | None:
    """Vrátí cachovaná data pokud jsou čerstvá, jinak None."""
    rows = conn.execute(
        "SELECT type_id, volume, best_sell, traded_volume, cached_at FROM station_volume_cache WHERE location_id=?",
        (location_id,)
    ).fetchall()
    if not rows:
        return None
    now = time.time()
    if any((now - (r[4] or 0)) > STATION_VOLUME_TTL for r in rows):
        return None
    return {r[0]: (r[1], r[2], r[3]) for r in rows}
