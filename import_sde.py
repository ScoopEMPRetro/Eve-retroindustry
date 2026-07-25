"""
Import CCP SDE data into a SQLite database.
Parses fsd/blueprints.yaml and fsd/types.yaml.
Usage: python import_sde.py
"""
import re
import yaml
import sqlite3
import os
import time
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

# Matches "1% reduction in manufacturing time" or "...in reaction time".
# Reactions skill (45746) has "...reaction time per skill level" — without
# this alternation it would be silently dropped from sde_skill_time_bonus.
_BONUS_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*%\s*reduction\s+in\s+(?:manufacturing|reaction)\s+time',
    re.IGNORECASE,
)

console = Console()

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
# The new CCP SDE (build 3417089+) has files in the zip root, not in fsd/. Support
# both layouts — data/fsd/ (older) and data/ (new).
SDE_DIR = os.path.join(_DATA_DIR, "fsd") \
    if os.path.exists(os.path.join(_DATA_DIR, "fsd", "types.yaml")) else _DATA_DIR
DB_PATH = os.path.join(os.path.dirname(__file__), "eve_cache.db")


def _yaml_load(f):
    """Load YAML via the libyaml C loader if available (orders of magnitude faster
    on the large types.yaml ~150 MB), otherwise the pure-Python SafeLoader."""
    try:
        from yaml import CSafeLoader as _Loader
    except ImportError:
        from yaml import SafeLoader as _Loader
    return yaml.load(f, Loader=_Loader)


BLUEPRINTS_YAML = os.path.join(SDE_DIR, "blueprints.yaml")
TYPES_YAML = os.path.join(SDE_DIR, "types.yaml")
GROUPS_YAML = os.path.join(SDE_DIR, "groups.yaml")


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sde_types (
            type_id         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            group_id        INTEGER,
            published       INTEGER DEFAULT 1,
            market_group_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_materials (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,   -- manufacturing / reaction
            material_type_id   INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            PRIMARY KEY (blueprint_type_id, activity, material_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_products (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            product_type_id    INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            probability        REAL DEFAULT 1.0,
            PRIMARY KEY (blueprint_type_id, activity, product_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprints (
            blueprint_type_id  INTEGER PRIMARY KEY,
            max_production_limit INTEGER DEFAULT 1,
            manufacturing_time   INTEGER DEFAULT 0,
            reaction_time        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_skills (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            skill_type_id      INTEGER NOT NULL,
            required_level     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (blueprint_type_id, activity, skill_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_skill_time_bonus (
            skill_type_id   INTEGER PRIMARY KEY,
            skill_name      TEXT NOT NULL,
            time_bonus_pct  REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_bp_product ON sde_blueprint_products(product_type_id);
        CREATE INDEX IF NOT EXISTS idx_bp_materials ON sde_blueprint_materials(blueprint_type_id, activity);
        CREATE INDEX IF NOT EXISTS idx_bp_skills ON sde_blueprint_skills(blueprint_type_id, activity);
    """)
    conn.commit()


def import_types(conn: sqlite3.Connection) -> dict:
    """Returns parsed types_data for reuse in skill bonus import."""
    console.print("[cyan]Loading types.yaml (147 MB, this takes a while)...[/]")
    t0 = time.time()

    with open(TYPES_YAML, "r", encoding="utf-8") as f:
        data = _yaml_load(f)

    console.print(f"[dim]YAML loaded in {time.time()-t0:.1f}s, importing {len(data):,} types...[/]")

    rows = []
    for type_id, info in data.items():
        if not isinstance(info, dict):
            continue
        name_field = info.get("name", {})
        name = name_field.get("en", "") if isinstance(name_field, dict) else str(name_field)
        if not name:
            continue
        rows.append((
            int(type_id),
            name,
            info.get("groupID"),
            1 if info.get("published", True) else 0,
            info.get("marketGroupID"),
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO sde_types (type_id, name, group_id, published, market_group_id)"
        " VALUES (?,?,?,?,?)",
        rows
    )
    conn.commit()
    console.print(f"[green]Imported {len(rows):,} types[/]")
    return data


_SKILL_EXCLUDE = {3380, 3388}  # Handled separately in calc_job_time
_IMPLANT_GROUP  = 743           # Zainou/manufacturing implants — not fetchable via ESI skills


def import_groups(conn: sqlite3.Connection):
    """Import groups.yaml → sde_groups (group_id, name en).

    Previously sde_groups was populated once via ESI (_ensure_groups_populated),
    which meant new groups (e.g. 5120 Command Carrier from Cradle of War)
    were never backfilled for existing users — rig_applies_to_product then
    returned False through its INNER JOIN and no rig applied to products from
    those groups.
    """
    if not os.path.exists(GROUPS_YAML):
        console.print(f"[yellow]groups.yaml not found ({GROUPS_YAML}) — skipping[/]")
        return
    console.print("Loading groups.yaml…")
    with open(GROUPS_YAML, "r", encoding="utf-8") as f:
        groups = _yaml_load(f)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sde_groups (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL
        );
    """)
    rows = []
    for gid, g in groups.items():
        name = (g.get("name") or {}).get("en") or f"Group {gid}"
        rows.append((int(gid), name))
    conn.executemany(
        "INSERT OR REPLACE INTO sde_groups (group_id, name) VALUES (?,?)", rows
    )
    conn.commit()
    console.print(f"  sde_groups: {len(rows)} groups")


def import_skill_time_bonuses(conn: sqlite3.Connection, types_data: dict):
    """Populate sde_skill_time_bonus from type descriptions."""
    rows = []
    for type_id, info in types_data.items():
        if not isinstance(info, dict):
            continue
        tid = int(type_id)
        if tid in _SKILL_EXCLUDE:
            continue
        if info.get("groupID") == _IMPLANT_GROUP:
            continue
        desc_field = info.get("description", {})
        desc_en = desc_field.get("en", "") if isinstance(desc_field, dict) else str(desc_field)
        m = _BONUS_RE.search(desc_en)
        if not m:
            continue
        bonus_pct = float(m.group(1))
        name_field = info.get("name", {})
        name = name_field.get("en", "") if isinstance(name_field, dict) else str(name_field)
        rows.append((tid, name, bonus_pct))

    conn.execute("DELETE FROM sde_skill_time_bonus")
    conn.executemany(
        "INSERT OR REPLACE INTO sde_skill_time_bonus VALUES (?,?,?)", rows
    )
    conn.commit()
    console.print(f"[green]Imported {len(rows)} skills with a time bonus[/]")


def import_blueprints(conn: sqlite3.Connection):
    console.print("[cyan]Loading blueprints.yaml...[/]")

    with open(BLUEPRINTS_YAML, "r", encoding="utf-8") as f:
        data = _yaml_load(f)

    console.print(f"[dim]Importing {len(data):,} blueprints...[/]")

    bp_rows, mat_rows, prod_rows, skill_rows = [], [], [], []

    for bp_type_id, info in data.items():
        if not isinstance(info, dict):
            continue

        activities = info.get("activities", {})
        max_limit = info.get("maxProductionLimit", 1)

        mfg_time = activities.get("manufacturing", {}).get("time", 0) if "manufacturing" in activities else 0
        rxn_time = activities.get("reaction", {}).get("time", 0) if "reaction" in activities else 0

        bp_rows.append((int(bp_type_id), max_limit, mfg_time, rxn_time))

        for activity_name in ("manufacturing", "reaction"):
            activity = activities.get(activity_name)
            if not activity:
                continue

            for mat in activity.get("materials") or []:
                mat_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(mat["typeID"]),
                    int(mat["quantity"]),
                ))

            for prod in activity.get("products") or []:
                prod_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(prod["typeID"]),
                    int(prod.get("quantity", 1)),
                    float(prod.get("probability", 1.0)),
                ))

            for skill in activity.get("skills") or []:
                skill_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(skill["typeID"]),
                    int(skill.get("level", 1)),
                ))

    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprints VALUES (?,?,?,?)",
        bp_rows
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_materials VALUES (?,?,?,?)",
        mat_rows
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_products VALUES (?,?,?,?,?)",
        prod_rows
    )
    conn.execute("DELETE FROM sde_blueprint_skills")
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_skills VALUES (?,?,?,?)",
        skill_rows
    )
    conn.commit()

    console.print(f"[green]Imported: {len(bp_rows):,} blueprints, "
                  f"{len(mat_rows):,} material rows, "
                  f"{len(prod_rows):,} product rows, "
                  f"{len(skill_rows):,} skill rows[/]")


def main():
    console.print("[bold]EVE Retroindustry — Import SDE into SQLite[/]\n")

    if not os.path.exists(BLUEPRINTS_YAML):
        console.print(f"[red]Not found: {BLUEPRINTS_YAML}[/]")
        return
    if not os.path.exists(TYPES_YAML):
        console.print(f"[red]Not found: {TYPES_YAML}[/]")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    t_start = time.time()
    types_data = import_types(conn)
    import_skill_time_bonuses(conn, types_data)
    import_blueprints(conn)
    import_groups(conn)
    conn.close()

    console.print(f"\n[bold green]Done in {time.time()-t_start:.1f}s[/]")
    console.print(f"Database: {DB_PATH}")

    # Quick test — Nidhoggur
    console.print("\n[bold]Test — Nidhoggur (24483):[/]")
    conn = sqlite3.connect(DB_PATH)

    # Find the blueprint for Nidhoggur
    bp = conn.execute(
        "SELECT blueprint_type_id FROM sde_blueprint_products WHERE product_type_id=? AND activity='manufacturing'",
        (24483,)
    ).fetchone()

    if bp:
        bp_id = bp[0]
        bp_name = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (bp_id,)).fetchone()
        console.print(f"  Blueprint: {bp_name[0] if bp_name else '?'} (ID: {bp_id})")

        materials = conn.execute("""
            SELECT t.name, m.quantity
            FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id=? AND m.activity='manufacturing'
            ORDER BY m.quantity DESC
        """, (bp_id,)).fetchall()

        console.print(f"  Materials ({len(materials)}):")
        for name, qty in materials:
            console.print(f"    - {name}: {qty:,}")
    else:
        console.print("  [red]Blueprint not found[/]")

    conn.close()


if __name__ == "__main__":
    main()
