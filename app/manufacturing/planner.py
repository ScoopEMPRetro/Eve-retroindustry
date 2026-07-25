"""
Manufacturing planner — compares the BOM with assets available at a station.

Modes:
  full       – cost of raw materials (the whole manufacturing chain)
  components – cost of the direct first-level components (Capital Armor Plates, etc.)
  optimal    – make vs. buy optimization (requires prices)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import sqlite3

from app.bom.resolver import BOMResolver, BOMNode, StationFacility
from app.bom.optimizer import optimize, get_shopping_list
from app.character.blueprints import CharBlueprint

PlanMode = Literal["full", "components", "optimal"]

# Skill type IDs
_SKILL_INDUSTRY        = 3380  # -4 % time/level
_SKILL_ADV_INDUSTRY    = 3388  # -3 % time/level


def calc_job_time(
    base_time: int,
    runs: int,
    te: int,
    industry_level: int,
    adv_industry_level: int,
    facility_te_multiplier: float = 1.0,
    is_reaction: bool = False,
    science_skill_mult: float = 1.0,
) -> int:
    """Return the total job time in seconds (runs × time/run after bonuses).

    EVE Online formula:
      time/run = base_time
        × (1 − te × 0.01)               # Blueprint TE (0–20)
        × (1 − industry × 0.04)         # Industry skill (manufacturing only)
        × (1 − adv_industry × 0.03)     # Advanced Industry skill (manufacturing only)
        × science_skill_mult             # science skills required by the blueprint (precomputed)
        × facility_te_multiplier         # structure + rigs (precomputed)
    Reactions: Industry/AdvIndustry do not apply.
    """
    mult = 1.0 - min(te, 20) * 0.01
    if not is_reaction:
        mult *= 1.0 - min(industry_level, 5) * 0.04
        mult *= 1.0 - min(adv_industry_level, 5) * 0.03
    mult *= max(0.01, science_skill_mult)
    mult *= max(0.01, facility_te_multiplier)
    time_per_run = max(1, round(base_time * mult))
    return time_per_run * max(1, runs)


def format_duration(seconds: int) -> str:
    """Format seconds as 'Xd Yh Zm'."""
    if seconds <= 0:
        return "—"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


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
    # For optimal mode — to display make vs buy decisions
    opt_total_cost:    float | None = None
    opt_naive_cost:    float | None = None
    # [Decision, …] from the optimizer (optimal mode only) — the UI renders
    # the Make vs Buy table from them, and steps skip "buy" components.
    opt_decisions:     list | None = None


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
    mfg_facility: StationFacility | None = None,
    rxn_facility: StationFacility | None = None,
    runs_per_job: int | None = 1,
) -> ManufacturingPlan:
    db_conn = sqlite3.connect(db_path)

    bp = find_blueprint_for_product(blueprints, product_type_id, db_conn)
    me = bp.material_efficiency if bp else 0
    te = bp.time_efficiency     if bp else 0

    resolver = BOMResolver(db_path, blueprints=blueprints, runs_per_job=runs_per_job)
    root = resolver.resolve(
        product_type_id, quantity, me=float(me),
        mfg_facility=mfg_facility, rxn_facility=rxn_facility,
    )
    resolver.close()
    db_conn.close()

    product_name = root.name
    opt_total = opt_naive = None

    if mode == "full":
        items = root.aggregate_leaves()

    elif mode == "components":
        # Direct first-level components (children of the root)
        items = {}
        for child in root.children:
            prev = items.get(child.type_id, (child.name, 0))[1]
            items[child.type_id] = (child.name, prev + child.quantity)

    # Make-vs-buy analysis runs outside optimal mode too — in full/components it
    # is purely informational (the "Make vs Buy" tab) and affects neither the
    # shopping list nor the manufacturing steps.
    opt_decisions = None
    if prices:
        opt_result = optimize(root, prices)
        opt_total  = opt_result.total_cost
        opt_naive  = opt_result.naive_cost
        opt_decisions = opt_result.decisions

    if mode == "optimal":
        if not prices:
            # Without prices, fall back to full
            items = root.aggregate_leaves()
        else:
            # Shopping list: a mix of buy components + raw materials for make branches
            decisions_map = {d.type_id: d for d in opt_decisions}
            items = get_shopping_list(root, decisions_map)
    elif mode not in ("full", "components"):
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
        opt_decisions    = opt_decisions,
    )
