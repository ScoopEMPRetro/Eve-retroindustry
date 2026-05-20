"""Pomůcky pro výpočet výrobních poplatků EVE Online."""
from __future__ import annotations
import sqlite3
import time
import httpx

ESI_BASE = "https://esi.evetech.net/latest"

# SCC Surcharge — zvýšen 1. 2. 2024 z 1.5 % na 4.0 % (třetí navýšení od Viridian 2023)
_SCC = 0.04


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_rigs (
            location_id    INTEGER PRIMARY KEY,
            me_bonus_pct   REAL NOT NULL DEFAULT 0,
            updated_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            structure_type TEXT,
            rig1_type_id   INTEGER,
            rig2_type_id   INTEGER,
            rig3_type_id   INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rig_bonuses (
            type_id  INTEGER PRIMARY KEY,
            name     TEXT NOT NULL,
            set_size TEXT NOT NULL,
            category TEXT NOT NULL,
            me_bonus REAL NOT NULL DEFAULT 0,
            te_bonus REAL NOT NULL DEFAULT 0
        )
    """)
    # Migrate old station_rigs table (add columns if missing)
    for col, defn in [
        ("structure_type", "TEXT"),
        ("rig1_type_id", "INTEGER"),
        ("rig2_type_id", "INTEGER"),
        ("rig3_type_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE station_rigs ADD COLUMN {col} {defn}")
        except Exception:
            pass
    conn.commit()


# Group ID → (set_size, category) for Standup structure rigs
_RIG_GROUP_MAP: dict[int, tuple[str, str]] = {
    **{gid: ("M", "manufacturing") for gid in [
        1816, 1819, 1820, 1821, 1822, 1823, 1824, 1825,
        1826, 1827, 1828, 1829, 1830, 1831, 1832, 1833,
        1834, 1835, 1836, 1837, 1838, 1839, 1840, 1841,
    ]},
    **{gid: ("L", "manufacturing") for gid in [
        1850, 1851, 1852, 1853, 1854, 1855, 1856, 1857,
        1858, 1859, 1860, 1861, 1862,
    ]},
    **{gid: ("XL", "manufacturing") for gid in [1867, 1868, 1869]},
    **{gid: ("M", "reaction") for gid in [1933, 1934, 1935, 1936, 1937, 1938]},
    1939: ("L", "reaction"),
}

# Structure type → (set_size, category)
STRUCTURE_TYPE_MAP: dict[str, tuple[str, str]] = {
    "raitaru": ("M",  "manufacturing"),
    "azbel":   ("L",  "manufacturing"),
    "sotiyo":  ("XL", "manufacturing"),
    "athanor": ("M",  "reaction"),
    "tatara":  ("L",  "reaction"),
}

# Structure type → TE bonus (% reduction of job time)
STRUCTURE_TE_BONUS: dict[str, float] = {
    "raitaru": 15.0,
    "azbel":   20.0,
    "sotiyo":  30.0,
    "athanor":  0.0,
    "tatara":  25.0,
}

# Structure type → base ME bonus (%) — engineering complexes give 1% ME, refineries 0%
STRUCTURE_ME_BONUS: dict[str, float] = {
    "raitaru": 1.0,
    "azbel":   1.0,
    "sotiyo":  1.0,
    "athanor": 0.0,
    "tatara":  0.0,
}


def populate_rig_bonuses(conn: sqlite3.Connection) -> None:
    """Populate rig_bonuses from local SDE. No-op if already populated."""
    if conn.execute("SELECT COUNT(*) FROM rig_bonuses").fetchone()[0] > 0:
        return

    group_ids = list(_RIG_GROUP_MAP.keys())
    ph = ",".join("?" * len(group_ids))
    rows = conn.execute(
        f"SELECT type_id, name, group_id FROM sde_types WHERE group_id IN ({ph}) AND published=1",
        group_ids,
    ).fetchall()

    entries = []
    for type_id, name, group_id in rows:
        if "Standup" not in name:
            continue  # skip non-rig items accidentally in these groups

        set_size, category = _RIG_GROUP_MAP[group_id]
        n = name.lower()
        is_t2 = name.endswith(" II")
        is_thukker = "thukker" in n
        enhanced = is_t2 or is_thukker

        me_base = 2.4 if enhanced else 2.0
        te_base = 24.0 if enhanced else 20.0

        has_mat = "material efficiency" in n
        has_time = "time efficiency" in n
        has_both = "efficiency" in n and not has_mat and not has_time

        me_bonus = me_base if (has_mat or has_both) else 0.0
        te_bonus = te_base if (has_time or has_both) else 0.0

        entries.append((type_id, name, set_size, category, me_bonus, te_bonus))

    conn.executemany(
        "INSERT OR REPLACE INTO rig_bonuses (type_id, name, set_size, category, me_bonus, te_bonus) VALUES (?,?,?,?,?,?)",
        entries,
    )
    conn.commit()


def get_rig_types(conn: sqlite3.Connection, structure_type: str) -> list[dict]:
    """Return available rigs for the given structure type (raitaru/azbel/etc)."""
    mapping = STRUCTURE_TYPE_MAP.get(structure_type)
    if not mapping:
        return []
    set_size, category = mapping
    rows = conn.execute(
        "SELECT type_id, name, me_bonus, te_bonus FROM rig_bonuses WHERE set_size=? AND category=? ORDER BY name",
        (set_size, category),
    ).fetchall()
    return [{"type_id": r[0], "name": r[1], "me_bonus": r[2], "te_bonus": r[3]} for r in rows]


def save_station_rigs_full(
    conn: sqlite3.Connection,
    location_id: int,
    structure_type: str | None,
    rig1_type_id: int | None,
    rig2_type_id: int | None,
    rig3_type_id: int | None,
) -> float:
    """Save rig configuration for a station and return the computed ME bonus (%)."""
    rig_ids = [r for r in [rig1_type_id, rig2_type_id, rig3_type_id] if r]
    me_bonus = STRUCTURE_ME_BONUS.get(structure_type or "", 0.0)
    if rig_ids:
        # Query each unique rig type once, then sum counting duplicates
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        bonus_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, me_bonus FROM rig_bonuses WHERE type_id IN ({ph})", unique_ids
        ).fetchall()}
        me_bonus += sum(bonus_map.get(rid, 0.0) for rid in rig_ids)

    conn.execute(
        """INSERT OR REPLACE INTO station_rigs
           (location_id, me_bonus_pct, updated_at, structure_type, rig1_type_id, rig2_type_id, rig3_type_id)
           VALUES (?,?,?,?,?,?,?)""",
        (location_id, me_bonus, int(time.time()), structure_type or None,
         rig1_type_id or None, rig2_type_id or None, rig3_type_id or None),
    )
    conn.commit()
    return me_bonus


def get_station_rigs_full(conn: sqlite3.Connection, location_id: int) -> dict:
    """Return rig configuration for a station."""
    row = conn.execute(
        "SELECT me_bonus_pct, structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
        " FROM station_rigs WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row:
        return {"me_bonus_pct": 0.0, "structure_type": None, "rigs": [None, None, None]}
    return {
        "me_bonus_pct": float(row[0] or 0.0),
        "structure_type": row[1],
        "rigs": [row[2], row[3], row[4]],
    }


def get_station_te_multiplier(conn: sqlite3.Connection, location_id: int) -> float:
    """Vrátí kombinovaný multiplikátor doby výroby pro stanici (např. 0.79 = 21 % rychleji).

    Zahrnuje bonus struktury (Raitaru/Azbel/Sotiyo) a rigs (TE rig bonusy aplikovány
    multiplikativně).
    """
    ensure_industry_tables(conn)
    row = conn.execute(
        "SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
        " FROM station_rigs WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row:
        return 1.0

    structure_type = row[0] or ""
    structure_te_pct = STRUCTURE_TE_BONUS.get(structure_type, 0.0)
    multiplier = 1.0 - structure_te_pct / 100

    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    if rig_ids:
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        rig_te_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, te_bonus FROM rig_bonuses WHERE type_id IN ({ph})",
            unique_ids,
        ).fetchall()}
        for rid in rig_ids:
            te_b = rig_te_map.get(rid, 0.0) / 100
            multiplier *= (1.0 - te_b)

    return max(0.01, multiplier)  # nikdy nezáporné


def get_station_me_bonus(conn: sqlite3.Connection, location_id: int) -> float:
    """Vrátí uložený ME bonus (%) pro danou stanici/strukturu, nebo 0.0."""
    ensure_industry_tables(conn)
    row = conn.execute(
        "SELECT me_bonus_pct FROM station_rigs WHERE location_id=?", (location_id,)
    ).fetchone()
    return float(row[0]) if row else 0.0


def save_station_me_bonus(conn: sqlite3.Connection, location_id: int, me_bonus_pct: float):
    ensure_industry_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO station_rigs (location_id, me_bonus_pct, updated_at) VALUES (?,?,?)",
        (location_id, max(0.0, min(25.0, me_bonus_pct)), int(time.time()))
    )
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
        facility_tax = job.cost / EIV − SCI − SCC
    kde SCC = 0.04 (State Compensation Commission surcharge, zvýšen 1. 2. 2024).

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
        tax = cost / eiv - sci - _SCC
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
