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


# Map rig group_id → category tag (what kind of product it affects).
# Tags are matched against product group classification by name keywords.
_RIG_CATEGORY: dict[int, str] = {
    # === Manufacturing rigs ===
    # Equipment (ship modules, rigs, deployables, implants, cargo containers)
    1816: "EQUIPMENT", 1819: "EQUIPMENT", 1850: "EQUIPMENT",
    # Ammunition (charges, scripts)
    1820: "AMMO", 1821: "AMMO", 1851: "AMMO",
    # Drone and Fighter
    1822: "DRONE", 1823: "DRONE", 1852: "DRONE",
    # Basic Ships (T1)
    1824: "SHIP_S_BASIC", 1825: "SHIP_S_BASIC", 1853: "SHIP_S_BASIC",
    1826: "SHIP_M_BASIC", 1827: "SHIP_M_BASIC", 1854: "SHIP_M_BASIC",
    1828: "SHIP_L_BASIC", 1829: "SHIP_L_BASIC", 1855: "SHIP_L_BASIC",
    # Advanced Ships (T2/T3)
    1830: "SHIP_S_ADV", 1831: "SHIP_S_ADV", 1856: "SHIP_S_ADV",
    1832: "SHIP_M_ADV", 1833: "SHIP_M_ADV", 1857: "SHIP_M_ADV",
    1834: "SHIP_L_ADV", 1835: "SHIP_L_ADV", 1858: "SHIP_L_ADV",
    # Capital Ships
    1859: "SHIP_CAPITAL",
    # Components
    1836: "ADV_COMPONENT", 1837: "ADV_COMPONENT", 1860: "ADV_COMPONENT",
    1838: "CAP_COMPONENT", 1839: "CAP_COMPONENT", 1861: "CAP_COMPONENT",
    # Structures
    1840: "STRUCTURE", 1841: "STRUCTURE", 1862: "STRUCTURE",
    # XL rigs (cover broad categories)
    1867: "EQUIPMENT_OR_AMMO",   # Equipment + consumable
    1868: "ANY_SHIP",            # Any ship
    1869: "STRUCTURE_OR_COMPONENT",  # Structure + component
    # === Reactor rigs ===
    1933: "REACT_COMPOSITE", 1934: "REACT_COMPOSITE",
    1935: "REACT_HYBRID",    1936: "REACT_HYBRID",
    1937: "REACT_BIO",       1938: "REACT_BIO",
    1939: "REACT_ANY",
}


# Cache of product_group_id → set of rig category tags it belongs to.
# Populated lazily on first query.
_product_cat_cache: dict[int, frozenset[str]] = {}


def _classify_product_group(group_id: int, group_name: str) -> frozenset[str]:
    """Klasifikuje skupinu produktů podle názvu — vrátí množinu rig category tagů,
    pro které se rig bonus aplikuje na tento produkt.
    """
    if group_id in _product_cat_cache:
        return _product_cat_cache[group_id]

    n = group_name.lower()
    cats: set[str] = set()

    # === Ships ===
    if group_id in (25, 420, 31, 237, 1283):
        cats.add("SHIP_S_BASIC"); cats.add("ANY_SHIP")
    elif group_id in (26, 419, 28, 463, 1201, 543, 380):
        cats.add("SHIP_M_BASIC"); cats.add("ANY_SHIP")
    elif group_id in (27, 513, 941):
        cats.add("SHIP_L_BASIC"); cats.add("ANY_SHIP")
    elif group_id in (324, 834, 830, 831, 893, 1527, 541, 1305, 1534, 540):
        cats.add("SHIP_S_ADV"); cats.add("ANY_SHIP")
    elif group_id in (358, 832, 894, 906, 833, 963, 1202, 1972):
        cats.add("SHIP_M_ADV"); cats.add("ANY_SHIP")
    elif group_id in (900, 898, 902):
        cats.add("SHIP_L_ADV"); cats.add("ANY_SHIP")
    elif group_id in (547, 485, 1538, 659, 30, 883, 4594):
        cats.add("SHIP_CAPITAL"); cats.add("ANY_SHIP")

    # === Drones / Fighters ===
    elif "drone" in n or "fighter" in n:
        cats.add("DRONE")

    # === Ammunition ===
    elif (any(k in n for k in ("ammo", "missile", "charge", "crystal", "frequency",
                                "torpedo", "script", "rocket", "bomb", "scanner probe",
                                "interdiction probe", "interdiction nullifier", "condenser pack",
                                "command burst", "filament", "breacher pod"))
          and "launcher" not in n):
        cats.add("AMMO")
        cats.add("EQUIPMENT_OR_AMMO")
    elif group_id in (90, 384, 385, 386, 387, 388, 479, 481, 648, 1019, 476, 4088):
        # 4088 = Interdiction Burst Probes (Stasis Webification Probe etc.) — empirically AMMO
        cats.add("AMMO")
        cats.add("EQUIPMENT_OR_AMMO")

    # === Components ===
    elif group_id == 873:
        cats.add("CAP_COMPONENT")
    elif group_id in (913, 716, 964):  # Advanced components, data interfaces, hybrid tech
        cats.add("ADV_COMPONENT")
    elif group_id == 4096:  # Molecular-Forged Materials — reaction output + used as ADV_COMPONENT
        cats.add("ADV_COMPONENT")
        cats.add("REACT_ANY")
    elif group_id in (954, 956, 957, 958):  # T3 subsystems
        cats.add("ADV_COMPONENT")
    elif group_id in (334, 536, 1314):  # Construction Components, Structure Components, Unknown Components
        cats.add("STRUCTURE_OR_COMPONENT")
    elif group_id == 332:  # Tool
        cats.add("ADV_COMPONENT")  # Tools are advanced components per rig description
    elif group_id == 1136:  # Fuel Block
        cats.add("STRUCTURE")
        cats.add("STRUCTURE_OR_COMPONENT")

    # === Reaction outputs (Athanor/Tatara) ===
    elif group_id in (428, 429):  # Intermediate Materials (simple), Composite (complex moon reactions)
        cats.add("REACT_COMPOSITE")
        cats.add("REACT_ANY")
    elif group_id == 974:  # Hybrid Polymers
        cats.add("REACT_HYBRID")
        cats.add("REACT_ANY")
    elif group_id == 712:  # Biochemical Material
        cats.add("REACT_BIO")
        cats.add("REACT_ANY")

    # === Structures (Upwell + starbase + deployables) ===
    elif (group_id in (365, 397, 404, 413, 438, 444, 471, 815, 838, 839, 1106, 1404,
                       1406, 1657, 1287, 1408, 4744, 4736, 1012, 365, 311, 363,
                       1106, 815, 1322, 1415, 1430, 1321, 1106, 4810)
          or "structure" in n or "starbase" in n or "citadel" in n or "refinery" in n
          or "engineering complex" in n or "upwell" in n or "control tower" in n
          or "sovereignty hub" in n or "infrastructure hub" in n
          or n.startswith("mobile ") or "deployable" in n
          or "service module" in n or "claim unit" in n):
        cats.add("STRUCTURE")
        cats.add("STRUCTURE_OR_COMPONENT")

    # === Equipment (modules, ship rigs, implants, containers, deployable tools) ===
    elif (group_id == 300                       # Cyberimplant (implants)
          or group_id in (12, 340, 448, 649, 1212)  # Cargo containers
          or n.startswith("rig ")               # Ship rigs (Rig Armor, Rig Shield, ...)
          or group_id in (1232, 1233, 1234, 1308)  # More rig groups
          # Empirically verified Equipment groups via EVE Ref API
          or group_id in (
              1154,  # Signature Suppressor
              546,   # Mining Upgrade
              1988,  # Entropic Radiation Sink
              658,   # Cynosural Field Generator
              4174,  # Compressors (ore/gas)
              1533,  # Micro Jump Field Generators
              740,   # Cyber Electronic Systems (implants)
              1230,  # Cyber Scanning (implants)
              1273,  # Encounter Surveillance System
              1815,  # Titan Phenomena Generator
          )
          or "launcher" in n                    # All weapon launchers
          or any(k in n for k in (
              "shield", "armor", "hull", "plate", "membrane", "coating", "hardener",
              "extender", "recharger", "flux", "amplifier", "booster", "damage control",
              "capacitor", "propulsion", "overdrive", "nanofiber", "inertial",
              "warp", "stasis", "disruptor", "scrambler", "target painter",
              "sensor", "tracking", "ecm", "jammer", "signal", "remote", "salvager",
              "tractor", "cloak", "enhancer", "weapon", "laser", "energy", "smart bomb",
              "mining laser", "strip miner", "gas cloud", "analyzer", "scanner",
              "survey", "data miner", "expanded cargohold", "reinforced bulkhead",
              "automated", "passive", "siege module", "triage", "jump drive",
              "jump portal", "clone vat", "fighter support", "module", "command burst",
              "co-processor", "ballistic control", "gyrostabilizer", "heat sink",
              "magnetic field", "weapon upgrade", "burst projector", "entosis",
              "stabilizer", "auxiliary power", "power diagnostic", "power relay",
              "reactor control", "regenerative plating", "tool", "nanite repair",
              "warp accelerator", "interdiction sphere", "warp core stabilizer",
              "scanning upgrade", "burst projector", "remote", "mass entangler",
              "vorton projector", "drone link", "drone control",
              "drone damage", "drone navigation", "drone tracking"))):
        cats.add("EQUIPMENT")
        cats.add("EQUIPMENT_OR_AMMO")

    # === Reactions ===
    elif group_id == 429:
        cats.add("REACT_COMPOSITE"); cats.add("REACT_ANY")
    elif group_id == 974:
        cats.add("REACT_HYBRID"); cats.add("REACT_ANY")
    elif group_id in (712, 4096):
        cats.add("REACT_BIO"); cats.add("REACT_ANY")
    elif group_id == 428:
        # Intermediate Materials — simple reactions, applies to L Reactor only
        cats.add("REACT_ANY")

    result = frozenset(cats)
    _product_cat_cache[group_id] = result
    return result


def rig_applies_to_product(
    conn: sqlite3.Connection,
    rig_type_id: int,
    product_type_id: int,
) -> bool:
    """Vrátí True pokud daný rig poskytuje bonus na výrobu daného produktu.

    Filtruje rig bonusy podle EVE pravidel (Equipment rig se neaplikuje na lodě atd.).
    Pro neznámou kombinaci defaultuje na False (bezpečně neaplikovat) — raději drobné
    podhodnocení úspor než falešné nadhodnocení.
    """
    rig_group_row = conn.execute(
        "SELECT group_id FROM sde_types WHERE type_id=?", (rig_type_id,)
    ).fetchone()
    if not rig_group_row:
        return False
    rig_group_id = rig_group_row[0]
    rig_cat = _RIG_CATEGORY.get(rig_group_id)
    if not rig_cat:
        return False

    prod_row = conn.execute(
        "SELECT t.group_id, g.name FROM sde_types t"
        " JOIN sde_groups g ON g.group_id = t.group_id"
        " WHERE t.type_id=?",
        (product_type_id,),
    ).fetchone()
    if not prod_row:
        return False
    prod_cats = _classify_product_group(prod_row[0], prod_row[1])
    return rig_cat in prod_cats


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
    """[DEPRECATED pro výpočet] Vrátí "globální" TE multiplikátor stanice — aplikuje
    všechny rigy bez ohledu na kategorii produktu. Použije se jen pro souhrnný
    display % v hlavičce (kde stejně nemáme konkrétní produkt). Pro per-job
    výpočet použij `get_product_te_multiplier(...)`.
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
        sec_mult = get_station_security_multiplier(conn, location_id)
        unique_ids = list(set(rig_ids))
        ph = ",".join("?" * len(unique_ids))
        rig_te_map = {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, te_bonus FROM rig_bonuses WHERE type_id IN ({ph})",
            unique_ids,
        ).fetchall()}
        for rid in rig_ids:
            te_b = rig_te_map.get(rid, 0.0) * sec_mult / 100
            multiplier *= (1.0 - te_b)

    return max(0.01, multiplier)  # nikdy nezáporné


def get_product_te_multiplier(conn: sqlite3.Connection, facility, product_type_id: int) -> float:
    """Per-product TE multiplikátor — uplatní jen rigy aplikovatelné na kategorii
    produktu (Equipment TE rig nezrychluje stavbu lodi atd.).

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
    """Vrátí StationFacility pro daný location_id — structure role bonus,
    rig list (s ME/TE bonusy), a security multiplier.
    Pro NPC stanice / neznámé struktury vrací prázdnou facility (1.0 multiplier).
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

    sec_mult = get_station_security_multiplier(conn, location_id)
    return StationFacility(
        structure_pct=structure_pct,
        structure_te_pct=structure_te_pct,
        rigs=tuple(rigs),
        sec_multiplier=sec_mult,
    )


def get_station_me_multiplier(conn: sqlite3.Connection, location_id: int) -> float:
    """Vrátí kombinovaný ME multiplikátor stanice (např. 0.87 = 13 % úspora).

    Bonusy jsou stackované multiplikativně (per CCP):
        m = (1 − struct_role/100) × (1 − rig1×sec/100) × (1 − rig2×sec/100) × …
    kde struct_role je 1 % pro engineering complexes a 0 % pro rafinerie,
    a sec je 1.0 / 1.9 / 2.1 podle security statusu systému.
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
        sec_mult = get_station_security_multiplier(conn, location_id)
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
    """Efektivní ME úspora v procentech pro UI: (1 - multiplier) × 100.

    Tj. multiplikativně sloučená úspora, ne aritmetická suma bonusů.
    """
    return round((1.0 - get_station_me_multiplier(conn, location_id)) * 100, 4)


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
    Odvodí facility tax z charakterových výrobních jobů. Z CCP vzorce
        job.cost = EIV × (SCI × (1 − struct_cost_bonus) + facility_tax + SCC)
    plyne
        facility_tax = job.cost / EIV − SCI × (1 − struct_cost_bonus) − SCC
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
        cost_bonus = get_station_cost_bonus(conn, facility_id)
        tax = cost / eiv - sci * (1.0 - cost_bonus) - _SCC
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
