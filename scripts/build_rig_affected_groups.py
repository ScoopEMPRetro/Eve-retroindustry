#!/usr/bin/env python3
"""Generate the authoritative rig → affected-product-groups mapping.

Replaces the old name-based `_classify_product_group` heuristic (many false
positives) with data derived from EVE Ref reference-data:

  * group-restricted rigs (ships by size/tech, capital, components, structures,
    reactions) → the rig type's `engineering_rig_affected_group_ids` list, which
    is exact and complete (and picks up new groups the old lists missed);
  * category-restricted rigs (equipment/ammo/drone and the broad XL rigs) →
    every manufacturable product group in the rig's EVE category set
    (Module=7, Charge=8, Drone=18, Fighter=87, Ship=6) plus any explicit extra
    groups (cargo containers) the rig lists.

Output: app/web/rig_affected_groups.py  (committed; runtime is fully offline).
Regenerate after an SDE rebuild or when CCP adds rig sets / product groups.

Usage:  venv/bin/python scripts/build_rig_affected_groups.py
Requires network access to ref-data.everef.net.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDE = os.path.join(REPO, "sde_base.db")
OUT = os.path.join(REPO, "app", "web", "rig_affected_groups.py")
BASE = "https://ref-data.everef.net"

# EVE categories used by category-restricted rigs.
CAT_SHIP, CAT_MODULE, CAT_CHARGE, CAT_DRONE, CAT_FIGHTER = 6, 7, 8, 18, 87

# Rig group_id → how to resolve its affected product groups.
#   "groups"                → use the rig's EVE Ref engineering_rig_affected_group_ids
#   frozenset({categories}) → every manufacturable product group in those categories
#                             (union'd with any explicit groups the rig also lists)
CAT_EQUIP = frozenset({CAT_MODULE})
CAT_AMMO = frozenset({CAT_CHARGE})
CAT_DRONE_SET = frozenset({CAT_DRONE, CAT_FIGHTER})
CAT_ANY_SHIP = frozenset({CAT_SHIP})
CAT_EQUIP_CONSUM = frozenset({CAT_MODULE, CAT_CHARGE})  # XL Equipment & Consumable

RIG_STRATEGY: dict[int, object] = {
    # ── Manufacturing, M-set ──
    1816: CAT_EQUIP, 1819: CAT_EQUIP,           # Equipment
    1820: CAT_AMMO, 1821: CAT_AMMO,             # Ammunition
    1822: CAT_DRONE_SET, 1823: CAT_DRONE_SET,   # Drone & Fighter
    1824: "groups", 1825: "groups",             # Basic Small Ship
    1826: "groups", 1827: "groups",             # Basic Medium Ship
    1828: "groups", 1829: "groups",             # Basic Large Ship
    1830: "groups", 1831: "groups",             # Advanced Small Ship
    1832: "groups", 1833: "groups",             # Advanced Medium Ship
    1834: "groups", 1835: "groups",             # Advanced Large Ship
    1836: "groups", 1837: "groups",             # Advanced Component
    1838: "groups", 1839: "groups",             # Capital Component
    1840: "groups", 1841: "groups",             # Structure
    # ── Manufacturing, L-set ──
    1850: CAT_EQUIP, 1851: CAT_AMMO, 1852: CAT_DRONE_SET,
    1853: "groups", 1854: "groups", 1855: "groups",
    1856: "groups", 1857: "groups", 1858: "groups",
    1859: "groups",                             # Capital Ship
    1860: "groups", 1861: "groups", 1862: "groups",
    # ── Manufacturing, XL-set (broad) ──
    1867: CAT_EQUIP_CONSUM,                     # Equipment & Consumable
    1868: CAT_ANY_SHIP,                         # Ship (all)
    1869: "groups",                             # Structure & Component
    # ── Reaction, M/L-set ──
    1933: "groups", 1934: "groups",             # Composite Reactor
    1935: "groups", 1936: "groups",             # Hybrid Reactor
    1937: "groups", 1938: "groups",             # Biochemical Reactor
    1939: "groups",                             # L Reactor (any)
}


def _get(url: str):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def main() -> None:
    conn = sqlite3.connect(SDE)

    # Manufacturable product groups = groups of anything a blueprint produces.
    prod_groups = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT t.group_id FROM sde_blueprint_products p "
            "JOIN sde_types t ON t.type_id = p.product_type_id")
    }
    print(f"manufacturable product groups: {len(prod_groups)}")

    # category_id for each product group (parallel).
    def cat_of(gid):
        try:
            return gid, _get(f"{BASE}/groups/{gid}").get("category_id")
        except Exception:
            return gid, None

    group_cat: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for gid, cid in ex.map(cat_of, sorted(prod_groups)):
            if cid is not None:
                group_cat[gid] = cid
    print(f"resolved categories for {len(group_cat)} groups")

    # One representative published rig type per rig group.
    rep_type: dict[int, int] = {}
    for gid in RIG_STRATEGY:
        row = conn.execute(
            "SELECT type_id FROM sde_types WHERE group_id=? AND published=1 "
            "ORDER BY type_id LIMIT 1", (gid,)).fetchone()
        if row:
            rep_type[gid] = row[0]
    conn.close()

    # EVE Ref affected group list per rig group (flatten the mfg/reaction dict).
    def rig_groups(gid):
        tid = rep_type.get(gid)
        if not tid:
            return gid, []
        try:
            d = _get(f"{BASE}/types/{tid}")
            e = d.get("engineering_rig_affected_group_ids")
            if isinstance(e, dict):
                return gid, sorted({g for v in e.values() for g in v})
            return gid, (sorted(e) if isinstance(e, list) else [])
        except Exception:
            return gid, []

    ever_groups: dict[int, list[int]] = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for gid, gl in ex.map(rig_groups, list(RIG_STRATEGY)):
            ever_groups[gid] = gl

    # Build final mapping.
    mapping: dict[int, frozenset[int]] = {}
    for gid, strat in RIG_STRATEGY.items():
        explicit = set(ever_groups.get(gid, []))
        if strat == "groups":
            affected = explicit
        else:  # category set → all manufacturable groups in those categories, + explicit extras
            cats = strat
            affected = {g for g, c in group_cat.items() if c in cats} | explicit
        # keep only groups that are actually manufacturable products
        mapping[gid] = frozenset(affected & prod_groups)

    _write(mapping, group_cat)
    _summary(mapping, group_cat)


def _write(mapping, group_cat) -> None:
    lines = [
        '"""Authoritative rig group_id → affected product group_ids.',
        "",
        "GENERATED by scripts/build_rig_affected_groups.py from EVE Ref reference-data.",
        "Do not edit by hand — regenerate after an SDE rebuild.",
        '"""',
        "from __future__ import annotations",
        "",
        "RIG_AFFECTED_GROUPS: dict[int, frozenset[int]] = {",
    ]
    for gid in sorted(mapping):
        groups = ", ".join(str(g) for g in sorted(mapping[gid]))
        lines.append(f"    {gid}: frozenset({{{groups}}}),")
    lines.append("}")
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote", OUT)


def _summary(mapping, group_cat) -> None:
    print("\n=== per-rig-group affected count ===")
    for gid in sorted(mapping):
        print(f"  {gid}: {len(mapping[gid])} groups")


if __name__ == "__main__":
    main()
