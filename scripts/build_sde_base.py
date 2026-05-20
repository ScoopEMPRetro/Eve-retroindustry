"""
Build sde_base.db — a clean, SDE-only database for bundling with PyInstaller.

Copies the SDE tables from the current eve_cache.db and strips all user data.
Run this before building the PyInstaller package.

Usage: python scripts/build_sde_base.py
"""
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DB = os.path.join(ROOT, "eve_cache.db")
DST_DB = os.path.join(ROOT, "sde_base.db")

SDE_TABLES = {
    "sde_types",
    "sde_groups",
    "sde_blueprints",
    "sde_blueprint_materials",
    "sde_blueprint_products",
    "sde_blueprint_skills",
    "sde_skill_time_bonus",
    "rig_bonuses",
}


def main() -> None:
    if not os.path.exists(SRC_DB):
        print(f"ERROR: {SRC_DB} not found. Run import_sde.py first.")
        sys.exit(1)

    # Verify SDE tables are populated
    conn_src = sqlite3.connect(SRC_DB)
    count = conn_src.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
    if count == 0:
        print("ERROR: sde_types is empty. Run import_sde.py first.")
        conn_src.close()
        sys.exit(1)
    print(f"Source: {count} sde_types rows found.")

    if os.path.exists(DST_DB):
        os.remove(DST_DB)

    # Copy full db then delete user tables
    shutil.copy2(SRC_DB, DST_DB)
    conn_dst = sqlite3.connect(DST_DB)

    # Get all tables in the db
    all_tables = [
        r[0] for r in conn_dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]

    for tbl in all_tables:
        if tbl not in SDE_TABLES and tbl != "sqlite_sequence":
            conn_dst.execute(f"DROP TABLE IF EXISTS [{tbl}]")
            print(f"  Dropped user table: {tbl}")

    conn_dst.execute("VACUUM")
    conn_dst.commit()
    conn_dst.close()
    conn_src.close()

    size_mb = os.path.getsize(DST_DB) / 1_048_576
    print(f"\nCreated: {DST_DB} ({size_mb:.1f} MB)")
    print("Ready to bundle with PyInstaller.")


if __name__ == "__main__":
    main()
