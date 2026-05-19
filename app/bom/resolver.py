"""
Rekurzivní BOM (Bill of Materials) resolver pro Eve Online výrobu.

Výpočet množství s ME:
  runs = ceil(needed_qty / product_qty_per_run)
  total_material = max(runs, ceil(base_qty * runs * (1 - ME/100)))

Leaf node = typ bez blueprintu v SDE (minerály, PI, moon goo, ...)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from math import ceil
import sqlite3


@dataclass
class BOMNode:
    type_id: int
    name: str
    quantity: int           # potřebné množství od rodiče
    runs: int               # počet výrobních runů
    is_leaf: bool           # True = primární surovina (nelze dál rozkládat)
    activity: str           # "manufacturing" | "reaction" | "raw"
    blueprint_type_id: int | None
    children: list[BOMNode] = field(default_factory=list)

    def aggregate_leaves(self) -> dict[int, tuple[str, int]]:
        """Vrátí slovník {type_id: (name, total_qty)} pro všechny leaf uzly."""
        result: dict[int, tuple[str, int]] = {}
        self._collect_leaves(result)
        return result

    def _collect_leaves(self, acc: dict[int, tuple[str, int]]):
        if self.is_leaf:
            if self.type_id in acc:
                acc[self.type_id] = (acc[self.type_id][0], acc[self.type_id][1] + self.quantity)
            else:
                acc[self.type_id] = (self.name, self.quantity)
        for child in self.children:
            child._collect_leaves(acc)


class BOMResolver:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def get_type_name(self, type_id: int) -> str:
        row = self.conn.execute(
            "SELECT name FROM sde_types WHERE type_id=?", (type_id,)
        ).fetchone()
        return row["name"] if row else f"Unknown ({type_id})"

    def find_blueprint(self, product_type_id: int) -> sqlite3.Row | None:
        """Najde blueprint, který produkuje daný typ (manufacturing nebo reaction)."""
        return self.conn.execute("""
            SELECT p.blueprint_type_id, p.quantity AS product_qty, p.activity,
                   b.manufacturing_time, b.reaction_time
            FROM sde_blueprint_products p
            JOIN sde_blueprints b ON b.blueprint_type_id = p.blueprint_type_id
            WHERE p.product_type_id = ?
              AND p.activity IN ('manufacturing', 'reaction')
            LIMIT 1
        """, (product_type_id,)).fetchone()

    def get_materials(self, blueprint_type_id: int, activity: str) -> list[sqlite3.Row]:
        return self.conn.execute("""
            SELECT m.material_type_id, m.quantity, t.name
            FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id = ? AND m.activity = ?
        """, (blueprint_type_id, activity)).fetchall()

    def resolve(
        self,
        type_id: int,
        quantity: int,
        me: float = 0.0,        # Material Efficiency 0–10
        depth: int = 0,
        visited: set[int] | None = None,
    ) -> BOMNode:
        """
        Rekurzivně rozloží výrobu daného typu na primární suroviny.
        me: 0–10 (EVE ME level, snižuje materiály o me % zaokrouhleno nahoru)
        """
        if visited is None:
            visited = set()

        name = self.get_type_name(type_id)
        blueprint = self.find_blueprint(type_id)

        # Leaf: žádný blueprint nebo cyklická závislost
        if blueprint is None or type_id in visited:
            return BOMNode(
                type_id=type_id, name=name, quantity=quantity,
                runs=0, is_leaf=True, activity="raw",
                blueprint_type_id=None,
            )

        product_qty_per_run = blueprint["product_qty"]
        activity = blueprint["activity"]
        bp_type_id = blueprint["blueprint_type_id"]

        runs = ceil(quantity / product_qty_per_run)
        materials = self.get_materials(bp_type_id, activity)

        node = BOMNode(
            type_id=type_id, name=name, quantity=quantity,
            runs=runs, is_leaf=False, activity=activity,
            blueprint_type_id=bp_type_id,
        )

        visited = visited | {type_id}  # immutable kopie pro větve

        for mat in materials:
            mat_qty = self._apply_me(mat["quantity"], runs, me)
            child = self.resolve(
                type_id=mat["material_type_id"],
                quantity=mat_qty,
                me=me,
                depth=depth + 1,
                visited=visited,
            )
            node.children.append(child)

        return node

    @staticmethod
    def _apply_me(base_qty: int, runs: int, me: float) -> int:
        """
        Eve Online ME formula:
        total = max(runs, ceil(base_qty * runs * (1 - ME/100)))
        """
        adjusted = ceil(base_qty * runs * (1 - me / 100))
        return max(runs, adjusted)
