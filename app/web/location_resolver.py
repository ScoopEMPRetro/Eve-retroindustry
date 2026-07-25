"""Resolve a location_id to a station/structure name (shared between plan.py and web)."""
from __future__ import annotations
import asyncio
import sqlite3
import httpx
from app.esi.client import esi_client

ESI_BASE = "https://esi.evetech.net/latest"
_cache: dict[int, str] = {}
_sys_cache: dict[int, int] = {}   # location_id → solar_system_id
_SEM = asyncio.Semaphore(10)

# location_id → True if ESI returned 403 (no docking access). Kept in memory
# for the lifetime of the process so the same inaccessible structure isn't
# resolved over and over — a flood of 403 responses would otherwise exhaust the
# ESI error limit (HTTP 420).
_forbidden: set[int] = set()

# When ESI returns 420 ("error limited"), stop ALL further name-resolution
# attempts until this time (monotonic seconds). Protects against a cascading
# ban of other endpoints (e.g. /universe/ids/).
_error_limited_until: float = 0.0


def _is_error_limited() -> bool:
    import time
    return time.monotonic() < _error_limited_until


def _set_error_limited(seconds: float = 60.0) -> None:
    global _error_limited_until
    import time
    _error_limited_until = max(_error_limited_until, time.monotonic() + seconds)


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
    """Return the security_status for the given system. Caches the result
    permanently (sec status doesn't normally change — only FW state, which we ignore)."""
    ensure_location_name_table(conn)
    row = conn.execute(
        "SELECT security_status FROM solar_system_cache WHERE system_id=?",
        (system_id,),
    ).fetchone()
    if row and row[0] is not None:
        return row[0]

    try:
        async with esi_client() as client:
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
    """Synchronously read security from cache. Returns None if not cached."""
    row = conn.execute(
        "SELECT security_status FROM solar_system_cache WHERE system_id=?",
        (system_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def security_multiplier(sec_status: float | None, is_reaction: bool = False) -> float:
    """Per CCP: rig bonuses scale with security, but DIFFERENTLY for
    manufacturing and for reactions:

      manufacturing: highsec ×1.0, lowsec ×1.9, null/WH ×2.1
      reaction:      lowsec ×1.0, null/WH ×1.1  (reactions don't run in highsec)

    Verified against the EVE Ref API: Titanium Carbide in a null Tatara with a
    T2 ME rig → 2.4 % × 1.1 = 2.64 % (not ×2.1), in lowsec ×1.0.

    None → highsec fallback (1.0), so gathering ESI data doesn't block the calculation.
    """
    if sec_status is None:
        return 1.0
    if sec_status >= 0.5:
        return 1.0
    if sec_status > 0.0:
        return 1.0 if is_reaction else 1.9
    return 1.1 if is_reaction else 2.1


def get_station_security_multiplier(
    conn: sqlite3.Connection,
    location_id: int,
    is_reaction: bool = False,
) -> float:
    """Synchronously return the rig security multiplier for a station.

    Assumes solar_system_cache has been populated from /api/station-industry-info.
    """
    row = conn.execute(
        "SELECT solar_system_id FROM location_name_cache WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row or not row[0]:
        return 1.0
    return security_multiplier(get_cached_security(conn, row[0]), is_reaction)


async def get_region_for_location(conn: sqlite3.Connection, location_id: int, token: str | None = None) -> int | None:
    """Return the region_id for the given location_id. Caches the result in the DB."""
    ensure_location_name_table(conn)
    row = conn.execute(
        "SELECT solar_system_id, region_id FROM location_name_cache WHERE location_id=?",
        (location_id,)
    ).fetchone()

    if row and row[1]:
        return row[1]

    sys_id = row[0] if row else None

    # If we don't have a system_id, resolve the station
    if not sys_id:
        async with esi_client() as client:
            _, sys_id = await resolve_station_name(client, location_id, token)
        if sys_id:
            conn.execute(
                "INSERT OR IGNORE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?,?,?)",
                (location_id, str(location_id), sys_id)
            )
            conn.commit()

    if not sys_id:
        return None

    # system → constellation → region (2 ESI calls)
    try:
        async with esi_client() as client:
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
    """Return {location_id: solar_system_id} for records where solar_system_id is not NULL."""
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
    """Return (name, solar_system_id)."""
    if location_id in _cache:
        return _cache[location_id], _sys_cache.get(location_id)

    name = str(location_id)
    sys_id: int | None = None
    forbidden = False

    # Structure previously returned 403 → don't try again (saves error limit).
    if location_id in _forbidden:
        return f"[Private structure {location_id}]", None
    # ESI temporarily error-limited us (420) → return a placeholder without a request.
    if _is_error_limited():
        return name, None

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
                elif r.status_code == 420:
                    _set_error_limited()
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
                        name = f"[Private structure {location_id}]"
                        forbidden = True
                        _forbidden.add(location_id)
                    elif r.status_code == 420:
                        _set_error_limited()
        except Exception:
            pass

    # Player structures without a resolved name aren't cached in memory —
    # after re-login with esi-universe.read_structures.v1 they're retried.
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

    async with esi_client() as client:
        tasks = [resolve_station_name(client, lid, token) for lid in location_ids]
        results = await asyncio.gather(*tasks)

    name_map = {lid: name for lid, (name, _) in zip(location_ids, results)}

    if conn is not None:
        new_entries: dict[int, tuple[str, int | None]] = {}
        for lid, (name, sys_id) in zip(location_ids, results):
            stored = db_names.get(lid)
            got_real = name != str(lid)
            is_forbidden = name == f"[Private structure {lid}]"
            stored_stale = stored is None or stored == str(lid) or stored == f"[Private structure {lid}]"
            upgrading = got_real and not name.startswith("[") and stored is not None and stored.startswith("[")
            if got_real and not is_forbidden and (stored_stale or upgrading):
                # Store only the real name — 403 fallbacks aren't cached in the DB
                new_entries[lid] = (name, sys_id)
            elif stored and not is_forbidden and sys_id and db_sys.get(lid) != sys_id:
                new_entries[lid] = (stored, sys_id)
        if new_entries:
            save_location_names_to_db(conn, new_entries)

    return name_map
