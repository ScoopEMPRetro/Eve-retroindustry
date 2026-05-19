"""
Výrobní plánovač — porovná BOM s dostupnými assety na stanici.

Módy:
  full       – cena základních surovin (celý výrobní řetězec)
  components – cena přímých komponentů 1. úrovně (Capital Armor Plates atd.)
  optimal    – make vs. buy optimalizace (potřebuje prices)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import sqlite3

from app.bom.resolver import BOMResolver, BOMNode
from app.bom.optimizer import optimize, get_shopping_list
from app.character.blueprints import CharBlueprint

PlanMode = Literal["full", "components", "optimal"]


@dataclass
class MaterialStatus:
    type_id:    int
    name:       str
    required:   int
    available:  int
    missing:    int

    @property
    def ok(self) -> bool:
        return self.missing == 0

    @property
    def coverage_pct(self) -> float:
        if self.required == 0:
            return 100.0
        return min(100.0, self.available / self.required * 100)


@dataclass
class ManufacturingPlan:
    product_type_id:   int
    product_name:      str
    quantity:          int
    blueprint:         CharBlueprint | None
    me:                int
    te:                int
    location_id:       int
    mode:              PlanMode
    materials:         list[MaterialStatus]
    can_manufacture:   bool
    total_missing_types: int
    # Pro optimal mód — pro zobrazení make vs buy rozhodnutí
    opt_total_cost:    float | None = None
    opt_naive_cost:    float | None = None


def find_blueprint_for_product(
    blueprints: list[CharBlueprint],
    product_type_id: int,
    db_conn: sqlite3.Connection,
) -> CharBlueprint | None:
    row = db_conn.execute(
        """SELECT blueprint_type_id FROM sde_blueprint_products
           WHERE product_type_id=? AND activity IN ('manufacturing','reaction')
           LIMIT 1""",
        (product_type_id,)
    ).fetchone()
    if not row:
        return None
    bp_type_id = row[0]
    candidates = [b for b in blueprints if b.type_id == bp_type_id]
    if not candidates:
        return None
    candidates.sort(key=lambda b: (0 if b.is_original else 1, -b.material_efficiency))
    return candidates[0]


def _make_status(
    items: dict[int, tuple[str, int]],
    available_assets: dict[int, int],
) -> list[MaterialStatus]:
    result = []
    for type_id, (name, required) in sorted(items.items(), key=lambda x: x[1][0]):
        avail   = available_assets.get(type_id, 0)
        missing = max(0, required - avail)
        result.append(MaterialStatus(type_id=type_id, name=name,
                                     required=required, available=avail, missing=missing))
    return result


def build_plan(
    product_type_id: int,
    quantity: int,
    location_id: int,
    available_assets: dict[int, int],
    blueprints: list[CharBlueprint],
    db_path: str,
    mode: PlanMode = "full",
    prices: dict[int, tuple[float | None, float | None]] | None = None,
    facility_me_bonus: float = 0.0,
    rxn_me_bonus: float = 0.0,
) -> ManufacturingPlan:
    db_conn = sqlite3.connect(db_path)

    bp = find_blueprint_for_product(blueprints, product_type_id, db_conn)
    me = bp.material_efficiency if bp else 0
    te = bp.time_efficiency     if bp else 0

    resolver = BOMResolver(db_path)
    root = resolver.resolve(
        product_type_id, quantity, me=float(me),
        facility_me_bonus=facility_me_bonus, rxn_me_bonus=rxn_me_bonus,
    )
    resolver.close()
    db_conn.close()

    product_name = root.name
    opt_total = opt_naive = None

    if mode == "full":
        items = root.aggregate_leaves()

    elif mode == "components":
        # Přímé komponenty 1. úrovně (děti kořene)
        items = {}
        for child in root.children:
            prev = items.get(child.type_id, (child.name, 0))[1]
            items[child.type_id] = (child.name, prev + child.quantity)

    elif mode == "optimal":
        if not prices:
            # Bez cen fallback na full
            items = root.aggregate_leaves()
        else:
            opt_result = optimize(root, prices)
            opt_total  = opt_result.total_cost
            opt_naive  = opt_result.naive_cost
            # Nákupní seznam: mix buy komponentů + raw surovin pro make větve
            decisions_map = {d.type_id: d for d in opt_result.decisions}
            items = get_shopping_list(root, decisions_map)
    else:
        items = root.aggregate_leaves()

    materials      = _make_status(items, available_assets)
    missing_types  = sum(1 for m in materials if not m.ok)

    return ManufacturingPlan(
        product_type_id  = product_type_id,
        product_name     = product_name,
        quantity         = quantity,
        blueprint        = bp,
        me=me, te=te,
        location_id      = location_id,
        mode             = mode,
        materials        = materials,
        can_manufacture  = (missing_types == 0),
        total_missing_types = missing_types,
        opt_total_cost   = opt_total,
        opt_naive_cost   = opt_naive,
    )
