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
from app.esi.client import esi_client

ESI_BASE = "https://esi.evetech.net/latest"
JITA_REGION = 10000002   # The Forge
JITA_STATION = 60003760  # Jita 4-4 CNAP
PRICE_CACHE_TTL = 60 * 60 * 12  # 12 hodin
# Používá se POUZE pro UI freshness indikátor (green/red badge na /prices,
# `fresh` flag v API). Pro výpočty cen (`get_prices_for_ids`) se cache
# NEexpiruje — vždy se použije poslední načtená Jita / The Forge sell hodnota,
# bez ohledu na stáří. Plný refresh přes `/markets/{region}/orders/` trvá
# ~3 s, user obvykle refreshuje 1× denně.

_JITA_SEM = asyncio.Semaphore(10)
# 7-day history se tahá per-type (žádný bulk endpoint), takže je to dominantní
# část refreshe (~19k volání). Concurrency 30 = ~2.5× rychlejší než 10 (515 vs
# 204 req/s naměřeno) a přitom bezpečně pod ESI rate-limitem — od ~45 souběžných
# začne ESI vracet HTTP 420 (error-limit), což je pomalejší A poškozuje sdílený
# error budget celé appky. 30 drží nulu 420 s rezervou.
_HIST_SEM = asyncio.Semaphore(30)


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

    Best-effort: tohle je jen fallback odhad ceny. NIKDY neraisuje —
    při 420 (ESI error-limit), timeoutu či jiné chybě vrátí {}, takže
    pád ESI nikdy neshodí dashboard / plán. Volající prázdný dict zvládne.
    """
    try:
        r = await client.get(
            f"{ESI_BASE}/markets/prices/",
            params={"datasource": "tranquility"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        return {d["type_id"]: d for d in r.json()}
    except Exception:
        return {}


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
    async with _HIST_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/history/",
                params={"type_id": type_id, "datasource": "tranquility"},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            history = r.json()
            if isinstance(history, list) and history:
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

    orders_resp = None
    for attempt in range(3):
        try:
            async with _JITA_SEM:
                orders_resp = await client.get(
                    f"{ESI_BASE}/markets/{JITA_REGION}/orders/",
                    params={"type_id": type_id, "order_type": "all", "datasource": "tranquility"},
                    timeout=15,
                )
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < 2:
                await asyncio.sleep(2 ** attempt * 3)
                continue
            return None, None

        if orders_resp.status_code in (420, 429):
            retry_after = int(orders_resp.headers.get("Retry-After", 60))
            await asyncio.sleep(min(retry_after, 120))
            continue
        if orders_resp.status_code == 404:
            _save_cached_price(conn, type_id, None, None, None, None)
            return None, None
        if orders_resp.status_code != 200:
            if attempt < 2:
                await asyncio.sleep(5)
                continue
            return None, None
        break
    else:
        return None, None

    volume = await _fetch_jita_volume(client, type_id)

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


# ---------------------------------------------------------------------------
# Bulk Jita orders — stáhne všechny aktivní orders v regionu naráz (paginated)
# ---------------------------------------------------------------------------

async def _fetch_orders_page(
    client: httpx.AsyncClient,
    region_id: int,
    page: int,
) -> tuple[list[dict], int]:
    """Stáhne jednu stránku orders a vrátí (orders, x_pages)."""
    async with _JITA_SEM:
        for attempt in range(3):
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/{region_id}/orders/",
                    params={"order_type": "all", "datasource": "tranquility", "page": page},
                    timeout=30,
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 3)
                    continue
                return [], 0
            if r.status_code in (420, 429):
                retry_after = int(r.headers.get("Retry-After", 60))
                await asyncio.sleep(min(retry_after, 120))
                continue
            if r.status_code != 200:
                if attempt < 2:
                    await asyncio.sleep(5)
                    continue
                return [], 0
            return r.json(), int(r.headers.get("x-pages", 1))
        return [], 0


async def _fetch_all_region_orders(
    client: httpx.AsyncClient,
    region_id: int,
    progress_cb=None,
) -> list[list[dict]]:
    """Stáhne VŠECHNY stránky orders regionu paginovaně (paralelně, _JITA_SEM).
    Vrátí seznam stránek (každá = list orderů). progress_cb(done, total) po každé
    stránce. Sdíleno mezi region-bulk a station-bulk agregací."""
    first, total_pages = await _fetch_orders_page(client, region_id, 1)
    if not first and total_pages == 0:
        return []

    pages_data: list[list[dict]] = [first]
    if progress_cb:
        await _maybe_call(progress_cb, 1, total_pages)

    remaining = list(range(2, total_pages + 1))
    completed = [1]
    lock = asyncio.Lock()

    async def _one(p: int):
        page_data, _ = await _fetch_orders_page(client, region_id, p)
        async with lock:
            pages_data.append(page_data)
            completed[0] += 1
            if progress_cb:
                await _maybe_call(progress_cb, completed[0], total_pages)

    await asyncio.gather(*[_one(p) for p in remaining], return_exceptions=True)
    return pages_data


async def fetch_station_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    progress_cb=None,
) -> dict[int, tuple[int, float]]:
    """Bulk varianta pro konkrétní stanici: stáhne regionální ordery jednou
    (~stránky, ne ~19k per-type volání) a vrátí {type_id: (sell_volume_sum,
    best_sell)} agregované JEN pro sell ordery na dané location_id.

    Řádově rychlejší než per-type `_fetch_orders_for_type` pro velké type_ids."""
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    agg: dict[int, tuple[int, float]] = {}
    for page_orders in pages_data:
        for o in page_orders:
            if o.get("is_buy_order"):
                continue
            if o.get("location_id") != location_id:
                continue
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            vol = int(o.get("volume_remain", 0))
            cur = agg.get(tid)
            if cur is None:
                agg[tid] = (vol, price)
            else:
                agg[tid] = (cur[0] + vol, min(cur[1], price))
    return agg


async def fetch_region_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int = JITA_REGION,
    progress_cb=None,
) -> dict[int, dict]:
    """Stáhne VŠECHNY aktivní orders pro region paginovaně a agreguje
    per type_id: {type_id: {sell, buy, jita_available}}.

    Tohle je řádově efektivnější než per-type call: ~500 stránek vs. 19k volání.
    progress_cb(page, total_pages) zavoláno po každé stránce (pokud zadáno).
    """
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    if not pages_data:
        return {}

    # Agregace per type_id
    agg: dict[int, dict] = {}
    for page_orders in pages_data:
        for o in page_orders:
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            entry = agg.setdefault(tid, {"sell": None, "buy": None, "jita_available": 0})
            if o.get("is_buy_order"):
                if entry["buy"] is None or price > entry["buy"]:
                    entry["buy"] = price
            else:
                if entry["sell"] is None or price < entry["sell"]:
                    entry["sell"] = price
                if o.get("location_id") == JITA_STATION:
                    entry["jita_available"] += int(o.get("volume_remain", 0))
    return agg


async def _maybe_call(cb, *args):
    """Helper — callback může být sync nebo async."""
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


# Per-type orders na custom stanici (fáze A ve fetch_station_volumes). Běží
# sekvenčně před history fází (_HIST_SEM), takže se concurrency nesčítá — 30 je
# bezpečné pod ESI rate-limitem (stejně jako _HIST_SEM).
_STATION_SEM = asyncio.Semaphore(30)
STATION_VOLUME_TTL = 60 * 30
# Od tolika type_ids se ve fetch_station_volumes vyplatí bulk (jeden region
# download) místo per-type volání. Pod prahem je per-type lehčí a rychlejší.
_BULK_ORDERS_THRESHOLD = 1000
_region_cache: dict[int, int] = {}  # structure_id → region_id (in-memory)


async def get_region_for_structure(structure_id: int) -> int | None:
    """Zjistí region_id pro strukturu přes ESI (system→constellation→region). Cachuje v paměti."""
    if structure_id in _region_cache:
        return _region_cache[structure_id]
    try:
        async with esi_client() as client:
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

    async with esi_client() as client:
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

    # 7-day "volume" je REGIONÁLNÍ historie (ESI nezveřejňuje historii obchodů
    # pro player struktury). Tahej ji pro VŠECHNY požadované typy — i pro ty,
    # které ve struktuře zrovna nemají žádnou nabídku, jinak by u nich "prodáno
    # za 7 dní" chybělo, přestože v regionu se s nimi obchoduje.
    if region_id is None:
        # Try location_name_cache first (populated by location resolver in web layer)
        try:
            row = conn.execute(
                "SELECT region_id FROM location_name_cache WHERE location_id=?",
                (structure_id,)
            ).fetchone()
            if row and row[0]:
                region_id = row[0]
        except Exception:
            pass
    if region_id is None:
        region_id = await get_region_for_structure(structure_id)

    history_map: dict[int, int | None] = {}
    if region_id and our_type_ids:
        async with esi_client() as client:
            hist_tasks = {tid: _fetch_region_volume(client, region_id, tid) for tid in our_type_ids}
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

    # Fáze A (ceny): dvě strategie podle počtu typů.
    #  - málo typů → per-type volání (lehké, žádný 94MB region download); vhodné
    #    pro plan sell price (1 typ).
    #  - hodně typů → bulk regionální ordery jednou + filtr na stanici (~2 s
    #    místo ~37 s); crossover ~1000 typů (bulk má fixní ~2 s + 94MB overhead).
    order_map: dict[int, tuple] = {}
    if len(type_ids) >= _BULK_ORDERS_THRESHOLD:
        async with esi_client() as client:
            station_orders = await fetch_station_orders_bulk(client, region_id, location_id)
        for tid in type_ids:
            vs = station_orders.get(tid)
            # typ bez sell orderu na stanici → (0, None), konzistentní s per-type
            order_map[tid] = vs if vs is not None else (0, None)
    else:
        async with esi_client() as client:
            order_tasks = [_fetch_orders_for_type(client, region_id, location_id, tid) for tid in type_ids]
            order_results = await asyncio.gather(*order_tasks, return_exceptions=True)
        for tid, res in zip(type_ids, order_results):
            order_map[tid] = res if isinstance(res, tuple) else (None, None)

    # 7-day regionální volume pro VŠECHNY typy — i pro ty, co na stanici zrovna
    # nemají order (jinak by u nich "prodáno za 7 dní" chybělo).
    history_map: dict[int, int | None] = {}
    if type_ids:
        async with esi_client() as client:
            hist_tasks = [_fetch_region_volume(client, region_id, tid) for tid in type_ids]
            hist_results = await asyncio.gather(*hist_tasks, return_exceptions=True)
        for tid, res in zip(type_ids, hist_results):
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
    # Pokud jsou záznamy s volume>0 ale všechna traded_volume jsou NULL,
    # cache je neúplná (region nebyl znám při ukládání) — vynutíme refetch.
    has_stock = any(r[1] and r[1] > 0 for r in rows)
    all_traded_null = all(r[3] is None for r in rows)
    if has_stock and all_traded_null:
        return None
    return {r[0]: (r[1], r[2], r[3]) for r in rows}
