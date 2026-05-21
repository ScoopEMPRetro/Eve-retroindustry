"""Překlad location_id na jméno stanice/struktury (sdíleno mezi plan.py a web)."""
from __future__ import annotations
import asyncio
import sqlite3
import httpx

ESI_BASE = "https://esi.evetech.net/latest"
_cache: dict[int, str] = {}
_sys_cache: dict[int, int] = {}   # location_id → solar_system_id
_SEM = asyncio.Semaphore(10)


def ensure_location_name_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS location_name_cache (
            location_id    INTEGER PRIMARY KEY,
            name           TEXT NOT NULL,
            solar_system_id INTEGER
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(location_name_cache)")}
    if "solar_system_id" not in cols:
        conn.execute("ALTER TABLE location_name_cache ADD COLUMN solar_system_id INTEGER")
    if "region_id" not in cols:
        conn.execute("ALTER TABLE location_name_cache ADD COLUMN region_id INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS solar_system_cache (
            system_id       INTEGER PRIMARY KEY,
            security_status REAL,
            cached_at       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)
    conn.commit()


async def get_security_status(
    conn: sqlite3.Connection,
    system_id: int,
) -> float | None:
    """Vrátí security_status pro daný systém. Cachuje výsledek napevno
    (sec status se nemění běžně — jen FW state, který ignorujeme)."""
    ensure_location_name_table(conn)
    row = conn.execute(
        "SELECT security_status FROM solar_system_cache WHERE system_id=?",
        (system_id,),
    ).fetchone()
    if row and row[0] is not None:
        return row[0]

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ESI_BASE}/universe/systems/{system_id}/",
                params={"datasource": "tranquility"},
                timeout=10,
            )
        if r.status_code == 200:
            sec = r.json().get("security_status")
            if sec is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO solar_system_cache (system_id, security_status) VALUES (?,?)",
                    (system_id, float(sec)),
                )
                conn.commit()
                return float(sec)
    except Exception:
        pass
    return None


def get_cached_security(conn: sqlite3.Connection, system_id: int) -> float | None:
    """Synchronní čtení security z cache. Vrátí None pokud není cached."""
    row = conn.execute(
        "SELECT security_status FROM solar_system_cache WHERE system_id=?",
        (system_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def security_multiplier(sec_status: float | None) -> float:
    """Per CCP: rig bonusy v lowsec ×1.9, v null/WH ×2.1, jinak ×1.0.

    None → highsec fallback (1.0), aby se sběr ESI dat neblokoval výpočet.
    """
    if sec_status is None:
        return 1.0
    if sec_status >= 0.5:
        return 1.0
    if sec_status > 0.0:
        return 1.9
    return 2.1


def get_station_security_multiplier(
    conn: sqlite3.Connection,
    location_id: int,
) -> float:
    """Synchronně vrátí rig security multiplier pro stanici (1.0/1.9/2.1).

    Předpokládá, že solar_system_cache byla naplněna z /api/station-industry-info.
    """
    row = conn.execute(
        "SELECT solar_system_id FROM location_name_cache WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row or not row[0]:
        return 1.0
    return security_multiplier(get_cached_security(conn, row[0]))


async def get_region_for_location(conn: sqlite3.Connection, location_id: int, token: str | None = None) -> int | None:
    """Vrátí region_id pro daný location_id. Cachuje výsledek v DB."""
    ensure_location_name_table(conn)
    row = conn.execute(
        "SELECT solar_system_id, region_id FROM location_name_cache WHERE location_id=?",
        (location_id,)
    ).fetchone()

    if row and row[1]:
        return row[1]

    sys_id = row[0] if row else None

    # Pokud nemáme system_id, resolve stanici
    if not sys_id:
        async with httpx.AsyncClient() as client:
            _, sys_id = await resolve_station_name(client, location_id, token)
        if sys_id:
            conn.execute(
                "INSERT OR IGNORE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?,?,?)",
                (location_id, str(location_id), sys_id)
            )
            conn.commit()

    if not sys_id:
        return None

    # system → constellation → region (2 ESI volání)
    try:
        async with httpx.AsyncClient() as client:
            sys_r = await client.get(
                f"{ESI_BASE}/universe/systems/{sys_id}/",
                params={"datasource": "tranquility"}, timeout=8,
            )
            if sys_r.status_code != 200:
                return None
            constellation_id = sys_r.json().get("constellation_id")
            if not constellation_id:
                return None

            con_r = await client.get(
                f"{ESI_BASE}/universe/constellations/{constellation_id}/",
                params={"datasource": "tranquility"}, timeout=8,
            )
            if con_r.status_code != 200:
                return None
            region_id = con_r.json().get("region_id")

        if region_id:
            conn.execute(
                "UPDATE location_name_cache SET region_id=? WHERE location_id=?",
                (region_id, location_id)
            )
            conn.commit()
        return region_id
    except Exception:
        return None


def load_location_names_from_db(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT location_id, name FROM location_name_cache").fetchall()
    return {r[0]: r[1] for r in rows}


def load_location_sys_from_db(conn: sqlite3.Connection) -> dict[int, int]:
    """Vrátí {location_id: solar_system_id} pro záznamy kde solar_system_id není NULL."""
    rows = conn.execute(
        "SELECT location_id, solar_system_id FROM location_name_cache WHERE solar_system_id IS NOT NULL"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def locations_in_system(conn: sqlite3.Connection, solar_system_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT location_id, name FROM location_name_cache WHERE solar_system_id = ?",
        (solar_system_id,)
    ).fetchall()
    return [{"location_id": r[0], "name": r[1]} for r in rows]


def save_location_names_to_db(conn: sqlite3.Connection, entries: dict[int, tuple[str, int | None]]):
    """entries: {location_id: (name, solar_system_id | None)}"""
    conn.executemany(
        "INSERT OR REPLACE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?, ?, ?)",
        [(lid, name, sys_id) for lid, (name, sys_id) in entries.items()]
    )
    conn.commit()


async def resolve_station_name(
    client: httpx.AsyncClient,
    location_id: int,
    token: str | None = None,
) -> tuple[str, int | None]:
    """Vrátí (name, solar_system_id)."""
    if location_id in _cache:
        return _cache[location_id], _sys_cache.get(location_id)

    name = str(location_id)
    sys_id: int | None = None
    forbidden = False
    async with _SEM:
        try:
            if location_id < 1_000_000_000_000:
                r = await client.get(
                    f"{ESI_BASE}/universe/stations/{location_id}/",
                    params={"datasource": "tranquility"},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    name = data.get("name", name)
                    sys_id = data.get("system_id")
            else:
                if token:
                    r = await client.get(
                        f"{ESI_BASE}/universe/structures/{location_id}/",
                        params={"datasource": "tranquility"},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        name = data.get("name", name)
                        sys_id = data.get("solar_system_id")
                    elif r.status_code == 403:
                        name = f"[Privátní struktura {location_id}]"
                        forbidden = True
        except Exception:
            pass

    # Player struktury bez rozlišeného jména se necachují v paměti —
    # po re-loginu s esi-universe.read_structures.v1 se zkusí znovu.
    if not forbidden and (sys_id is not None or location_id < 1_000_000_000_000):
        _cache[location_id] = name
        if sys_id:
            _sys_cache[location_id] = sys_id
    return name, sys_id


async def resolve_station_names_bulk(
    location_ids: list[int],
    token: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[int, str]:
    if conn is not None:
        ensure_location_name_table(conn)
        db_names = load_location_names_from_db(conn)
        db_sys   = load_location_sys_from_db(conn)
        _cache.update(db_names)
        _sys_cache.update(db_sys)
    else:
        db_names = {}

    async with httpx.AsyncClient() as client:
        tasks = [resolve_station_name(client, lid, token) for lid in location_ids]
        results = await asyncio.gather(*tasks)

    name_map = {lid: name for lid, (name, _) in zip(location_ids, results)}

    if conn is not None:
        new_entries: dict[int, tuple[str, int | None]] = {}
        for lid, (name, sys_id) in zip(location_ids, results):
            stored = db_names.get(lid)
            got_real = name != str(lid)
            is_forbidden = name == f"[Privátní struktura {lid}]"
            stored_stale = stored is None or stored == str(lid) or stored == f"[Privátní struktura {lid}]"
            upgrading = got_real and not name.startswith("[") and stored is not None and stored.startswith("[")
            if got_real and not is_forbidden and (stored_stale or upgrading):
                # Uložíme jen reálné jméno — 403 fallbacky se necachují do DB
                new_entries[lid] = (name, sys_id)
            elif stored and not is_forbidden and sys_id and db_sys.get(lid) != sys_id:
                new_entries[lid] = (stored, sys_id)
        if new_entries:
            save_location_names_to_db(conn, new_entries)

    return name_map
