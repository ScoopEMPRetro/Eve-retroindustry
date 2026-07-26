"""Helpers for computing EVE Online manufacturing fees."""
from __future__ import annotations
import sqlite3
import time
import httpx
from app.esi.client import esi_client
# Authoritative rig group_id → affected product group_ids, generated from EVE Ref
# reference-data (see scripts/build_rig_affected_groups.py). Replaces the old
# name-based product classification, which produced ~74 false positives.
from app.web.rig_affected_groups import RIG_AFFECTED_GROUPS

ESI_BASE = "https://esi.evetech.net/latest"

# SCC Surcharge — raised on 2024-02-01 from 1.5 % to 4.0 % (third increase since Viridian 2023)
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


def rig_applies_to_product(
    conn: sqlite3.Connection,
    rig_type_id: int,
    product_type_id: int,
) -> bool:
    """Return True if the given rig provides a bonus to manufacturing the given product.

    Looks the product's group up in the rig's authoritative affected-groups set
    (RIG_AFFECTED_GROUPS, generated from EVE Ref). Unknown combination → False
    (safe: a slight underestimate of savings beats a false overestimate).
    """
    rig_group_row = conn.execute(
        "SELECT group_id FROM sde_types WHERE type_id=?", (rig_type_id,)
    ).fetchone()
    prod_row = conn.execute(
        "SELECT group_id FROM sde_types WHERE type_id=?", (product_type_id,)
    ).fetchone()
    if not rig_group_row or not prod_row:
        return False
    affected = RIG_AFFECTED_GROUPS.get(rig_group_row[0])
    return bool(affected) and prod_row[0] in affected


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

# Refineries run reactions — their rig bonuses scale with a DIFFERENT
# security table (lowsec ×1.0, null/WH ×1.1) than manufacturing rigs
# (lowsec ×1.9, null/WH ×2.1). Verified against EVE Ref API.
_RXN_STRUCTURE_TYPES = frozenset({"athanor", "tatara"})

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

# Structure type → job-installation cost bonus (fraction; reduces SCI portion).
# Engineering complex role bonus: Raitaru 3%, Azbel 4%, Sotiyo 5%.
# Refineries have no SCI cost bonus.
STRUCTURE_COST_BONUS: dict[str, float] = {
    "raitaru": 0.03,
    "azbel":   0.04,
    "sotiyo":  0.05,
    "athanor": 0.0,
    "tatara":  0.0,
}


def get_station_cost_bonus(conn: sqlite3.Connection, location_id: int) -> float:
    """Return SCI cost reduction fraction (e.g. 0.03 for Raitaru, 0.0 for NPC)."""
    ensure_industry_tables(conn)
    row = conn.execute(
        "SELECT structure_type FROM station_rigs WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row or not row[0]:
        return 0.0
    return STRUCTURE_COST_BONUS.get(row[0], 0.0)


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
    """[DEPRECATED for calculation] Return the station's "global" TE multiplier — applies
    all rigs regardless of product category. Used only for the summary
    display % in the header (where we don't have a specific product anyway). For per-job
    calculation use `get_product_te_multiplier(...)`.
    """
    from app.web.location_resolver import get_station_security_multiplier

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
        sec_mult = get_station_security_multiplier(
            conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
        )
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        rig_te_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, te_bonus FROM rig_bonuses WHERE type_id IN ({ph})",
            unique_ids,
        ).fetchall()}
        for rid in rig_ids:
            te_b = rig_te_map.get(rid, 0.0) * sec_mult / 100
            multiplier *= (1.0 - te_b)

    return max(0.01, multiplier)  # never negative


def get_product_te_multiplier(conn: sqlite3.Connection, facility, product_type_id: int) -> float:
    """Per-product TE multiplier — applies only rigs relevant to the product's
    category (an Equipment TE rig does not speed up ship construction, etc.).

    facility: StationFacility z app.bom.resolver (passed in to avoid circular import).
    """
    multiplier = 1.0 - facility.structure_te_pct / 100
    for rig_id, _me_b, te_b in facility.rigs:
        if te_b <= 0:
            continue
        if rig_applies_to_product(conn, rig_id, product_type_id):
            multiplier *= 1.0 - te_b * facility.sec_multiplier / 100
    return max(0.01, multiplier)


def get_station_facility(conn: sqlite3.Connection, location_id: int):
    """Return the StationFacility for the given location_id — structure role bonus,
    rig list (with ME/TE bonuses), and security multiplier.
    For NPC stations / unknown structures it returns an empty facility (1.0 multiplier).
    """
    from app.bom.resolver import StationFacility
    from app.web.location_resolver import get_station_security_multiplier

    ensure_industry_tables(conn)
    row = conn.execute(
        "SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
        " FROM station_rigs WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row:
        return StationFacility()

    structure_type = row[0] or ""
    structure_pct = STRUCTURE_ME_BONUS.get(structure_type, 0.0)
    structure_te_pct = STRUCTURE_TE_BONUS.get(structure_type, 0.0)
    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    rigs: list[tuple[int, float, float]] = []
    if rig_ids:
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        rig_map = {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT type_id, me_bonus, te_bonus FROM rig_bonuses WHERE type_id IN ({ph})",
            unique_ids,
        ).fetchall()}
        for rid in rig_ids:
            me_b, te_b = rig_map.get(rid, (0.0, 0.0))
            rigs.append((rid, me_b, te_b))

    sec_mult = get_station_security_multiplier(
        conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
    )
    return StationFacility(
        structure_pct=structure_pct,
        structure_te_pct=structure_te_pct,
        rigs=tuple(rigs),
        sec_multiplier=sec_mult,
    )


def get_station_me_multiplier(conn: sqlite3.Connection, location_id: int) -> float:
    """Return the station's combined ME multiplier (e.g. 0.87 = 13 % savings).

    Bonuses stack multiplicatively (per CCP):
        m = (1 − struct_role/100) × (1 − rig1×sec/100) × (1 − rig2×sec/100) × …
    where struct_role is 1 % for engineering complexes and 0 % for refineries,
    and sec is 1.0 / 1.9 / 2.1 depending on the system's security status.
    """
    from app.web.location_resolver import get_station_security_multiplier

    ensure_industry_tables(conn)
    row = conn.execute(
        "SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
        " FROM station_rigs WHERE location_id=?",
        (location_id,),
    ).fetchone()
    if not row:
        return 1.0

    structure_type = row[0] or ""
    structure_pct = STRUCTURE_ME_BONUS.get(structure_type, 0.0)
    multiplier = 1.0 - structure_pct / 100

    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    if rig_ids:
        sec_mult = get_station_security_multiplier(
            conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
        )
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        rig_me_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, me_bonus FROM rig_bonuses WHERE type_id IN ({ph})",
            unique_ids,
        ).fetchall()}
        for rid in rig_ids:
            me_b = rig_me_map.get(rid, 0.0) * sec_mult / 100
            multiplier *= (1.0 - me_b)

    return max(0.01, multiplier)


def get_station_me_bonus_pct(conn: sqlite3.Connection, location_id: int) -> float:
    """Effective ME savings as a percentage for the UI: (1 - multiplier) × 100.

    I.e. the multiplicatively combined savings, not the arithmetic sum of bonuses.
    """
    return round((1.0 - get_station_me_multiplier(conn, location_id)) * 100, 4)


def get_station_me_bonus(conn: sqlite3.Connection, location_id: int) -> float:
    """Return the stored ME bonus (%) for the given station/structure, or 0.0."""
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
    """Return {type_id: adjusted_price} from cache or ESI (GET /markets/prices/)."""
    ensure_industry_tables(conn)
    if _adj_is_fresh(conn):
        rows = conn.execute("SELECT type_id, adjusted FROM adjusted_price_cache").fetchall()
        return {r[0]: r[1] for r in rows}
    try:
        async with esi_client() as client:
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
    # Return stale cache if ESI fails
    rows = conn.execute("SELECT type_id, adjusted FROM adjusted_price_cache").fetchall()
    return {r[0]: r[1] for r in rows}


async def get_sci_for_system(
    conn: sqlite3.Connection,
    solar_system_id: int,
    activity: str,
) -> float:
    """
    Return the System Cost Index for the given system and activity.
    On a missing/expired cache it fetches the entire GET /industry/systems/ endpoint
    and stores all values at once.
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
        async with esi_client() as client:
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


