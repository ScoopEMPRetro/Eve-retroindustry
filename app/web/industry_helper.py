"""Pomůcky pro výpočet výrobních poplatků EVE Online."""
from __future__ import annotations
import sqlite3
import time
import httpx

ESI_BASE = "https://esi.evetech.net/latest"


def ensure_industry_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS adjusted_price_cache (
            type_id    INTEGER PRIMARY KEY,
            adjusted   REAL NOT NULL,
            cached_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sci_cache (
            solar_system_id INTEGER NOT NULL,
            activity        TEXT NOT NULL,
            cost_index      REAL NOT NULL,
            cached_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (solar_system_id, activity)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facility_tax_cache (
            facility_id INTEGER PRIMARY KEY,
            tax_rate    REAL NOT NULL,
            cached_at   INTEGER NOT NULL
        )
    """)
    conn.commit()


def _adj_is_fresh(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT MIN(cached_at) FROM adjusted_price_cache").fetchone()
    if not row or row[0] is None:
        return False
    return (time.time() - row[0]) < 86400  # 24 h


def _sci_is_fresh(conn: sqlite3.Connection, solar_system_id: int, activity: str) -> bool:
    row = conn.execute(
        "SELECT cached_at FROM sci_cache WHERE solar_system_id=? AND activity=?",
        (solar_system_id, activity),
    ).fetchone()
    if not row:
        return False
    return (time.time() - row[0]) < 3600  # 1 h


async def get_adjusted_prices(conn: sqlite3.Connection) -> dict[int, float]:
    """Vrátí {type_id: adjusted_price} z cache nebo ESI (GET /markets/prices/)."""
    ensure_industry_tables(conn)
    if _adj_is_fresh(conn):
        rows = conn.execute("SELECT type_id, adjusted FROM adjusted_price_cache").fetchall()
        return {r[0]: r[1] for r in rows}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ESI_BASE}/markets/prices/",
                params={"datasource": "tranquility"},
                timeout=30,
            )
        if r.status_code == 200:
            now = int(time.time())
            entries = [
                (item["type_id"], item["adjusted_price"], now)
                for item in r.json()
                if item.get("adjusted_price") is not None
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO adjusted_price_cache (type_id, adjusted, cached_at) VALUES (?,?,?)",
                entries,
            )
            conn.commit()
            return {e[0]: e[1] for e in entries}
    except Exception:
        pass
    # Vrátí stale cache pokud ESI selže
    rows = conn.execute("SELECT type_id, adjusted FROM adjusted_price_cache").fetchall()
    return {r[0]: r[1] for r in rows}


async def get_sci_for_system(
    conn: sqlite3.Connection,
    solar_system_id: int,
    activity: str,
) -> float:
    """
    Vrátí System Cost Index pro daný systém a aktivitu.
    Při chybějící/expirované cache stáhne celý GET /industry/systems/ endpoint
    a uloží všechny hodnoty najednou.
    """
    ensure_industry_tables(conn)
    if _sci_is_fresh(conn, solar_system_id, activity):
        row = conn.execute(
            "SELECT cost_index FROM sci_cache WHERE solar_system_id=? AND activity=?",
            (solar_system_id, activity),
        ).fetchone()
        if row:
            return row[0]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ESI_BASE}/industry/systems/",
                params={"datasource": "tranquility"},
                timeout=30,
            )
        if r.status_code == 200:
            now = int(time.time())
            entries = [
                (sys["solar_system_id"], idx["activity"], idx["cost_index"], now)
                for sys in r.json()
                for idx in sys.get("cost_indices", [])
            ]
            conn.executemany(
                "INSERT OR REPLACE INTO sci_cache"
                " (solar_system_id, activity, cost_index, cached_at) VALUES (?,?,?,?)",
                entries,
            )
            conn.commit()
    except Exception:
        pass
    row = conn.execute(
        "SELECT cost_index FROM sci_cache WHERE solar_system_id=? AND activity=?",
        (solar_system_id, activity),
    ).fetchone()
    return row[0] if row else 0.0


def _last_downtime() -> "datetime":
    """EVE server downtime ≈ 11:00 UTC denně. SCI se aktualizuje při každém downtimeu."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    dt  = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if dt > now:
        dt -= timedelta(days=1)
    return dt


async def derive_facility_tax(
    conn: sqlite3.Connection,
    facility_id: int,
    char_id: int,
    token: str,
) -> float | None:
    """
    Odvodí facility tax z charakterových výrobních jobů:
        facility_tax = job.cost / EIV − SCI

    Spolehlivost:
    - Používá POUZE joby spuštěné PO posledním downtimeu (SCI epoch shoda → přesné).
    - Pokud žádný takový job neexistuje, vrátí naposledy uložený výsledek z cache
      (facility tax se mění zřídka, uložená hodnota je obvykle stále správná).
    - Pokud cache je prázdná, vrátí None → uživatel zadá ručně.
    """
    from datetime import datetime, timezone
    ensure_industry_tables(conn)

    # Čerstvá cache < 6 hodin → rovnou vrátit
    cached = conn.execute(
        "SELECT tax_rate, cached_at FROM facility_tax_cache WHERE facility_id=?",
        (facility_id,),
    ).fetchone()
    if cached and (time.time() - cached[1]) < 21600:
        return cached[0]

    # Fetch jobů z ESI
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ESI_BASE}/characters/{char_id}/industry/jobs/",
                params={"datasource": "tranquility", "include_completed": "true"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        if r.status_code != 200:
            return cached[0] if cached else None
        all_jobs = r.json()
    except Exception:
        return cached[0] if cached else None

    ACTIVITY_MAP = {1: "manufacturing", 9: "reaction"}
    last_dt = _last_downtime()

    # Pouze joby ze stejného SCI epochu (spuštěné po posledním downtimeu)
    epoch_jobs = [
        j for j in all_jobs
        if j.get("facility_id") == facility_id
        and j.get("activity_id") in ACTIVITY_MAP
        and (j.get("cost") or 0) > 0
        and datetime.fromisoformat(
            j["start_date"].replace("Z", "+00:00")
        ) >= last_dt
    ]

    if not epoch_jobs:
        # Žádný job ze stejného SCI epochu → vrátit stale cache (facility tax se nemění)
        return cached[0] if cached else None

    adj_prices = await get_adjusted_prices(conn)

    sys_row = conn.execute(
        "SELECT solar_system_id FROM location_name_cache WHERE location_id=?",
        (facility_id,),
    ).fetchone()
    sys_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

    derived: list[float] = []
    for job in epoch_jobs:
        act_name = ACTIVITY_MAP[job["activity_id"]]
        bp_id    = job["blueprint_type_id"]
        runs     = max(job.get("runs", 1) or 1, 1)
        cost     = job["cost"]

        mats = conn.execute(
            "SELECT material_type_id, quantity FROM sde_blueprint_materials "
            "WHERE blueprint_type_id=? AND activity=?",
            (bp_id, act_name),
        ).fetchall()
        if not mats:
            continue

        eiv = sum(adj_prices.get(mid, 0.0) * qty * runs for mid, qty in mats)
        if eiv < 50_000:
            continue  # Příliš nízká EIV → hlučný výsledek

        sci = await get_sci_for_system(conn, sys_id, act_name) if sys_id else 0.0
        tax = cost / eiv - sci
        if 0.0 <= tax <= 0.25:
            derived.append(tax)

    if not derived:
        return cached[0] if cached else None

    # Medián — robustní vůči odlehlým hodnotám (různé blueprinty, různé materiály)
    derived.sort()
    n   = len(derived)
    mid = n // 2
    result = derived[mid] if n % 2 else (derived[mid - 1] + derived[mid]) / 2

    conn.execute(
        "INSERT OR REPLACE INTO facility_tax_cache (facility_id, tax_rate, cached_at) VALUES (?,?,?)",
        (facility_id, result, int(time.time())),
    )
    conn.commit()
    return result
