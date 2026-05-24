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

from app.character.blueprints import CharBlueprint


@dataclass(frozen=True)
class StationFacility:
    """Strukturovaná konfigurace stanice pro výpočet ME/TE multiplikátorů per produkt.

    `structure_pct` = ME role bonus struktury (např. 1.0 % pro engineering complex).
    `structure_te_pct` = TE role bonus struktury (15/20/30/0/25 % pro Raitaru/Azbel/Sotiyo/Athanor/Tatara).
    `rigs` = seznam (rig_type_id, me_bonus_pct, te_bonus_pct).
    `sec_multiplier` = 1.0 / 1.9 / 2.1 podle security statusu systému.

    Resolver / time-calc pak per produkt rozhoduje, které rigy se uplatní (Equipment rig
    se neaplikuje na lodě atd.) — viz industry_helper.rig_applies_to_product.
    """
    structure_pct: float = 0.0
    structure_te_pct: float = 0.0
    rigs: tuple[tuple[int, float, float], ...] = ()
    sec_multiplier: float = 1.0


@dataclass
class BOMNode:
    type_id: int
    name: str
    quantity: int           # potřebné množství od rodiče
    runs: int               # počet výrobních runů
    is_leaf: bool           # True = primární surovina (nelze dál rozkládat)
    activity: str           # "manufacturing" | "reaction" | "raw"
    blueprint_type_id: int | None
    me: int = 0             # efektivní ME použité pro výpočet (0 pokud user BP nemá)
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
    def __init__(self, db_path: str, blueprints: list[CharBlueprint] | None = None):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # product_type_id → ME postavy (best dostupný blueprint pro daný produkt)
        self._bp_me_by_product: dict[int, int] = {}
        if blueprints:
            self._build_bp_index(blueprints)

    def close(self):
        self.conn.close()

    def _build_bp_index(self, blueprints: list[CharBlueprint]) -> None:
        """Předpočítá nejlepší ME postavy pro každý vyrobitelný produkt.

        Pro produkty s více blueprinty (BPO + BPC, nebo BPO + různé kopie)
        vybírá BPO před BPC, pak nejvyšší ME.
        """
        bp_type_ids = list({bp.type_id for bp in blueprints})
        if not bp_type_ids:
            return
        ph = ",".join("?" * len(bp_type_ids))
        rows = self.conn.execute(
            f"""SELECT blueprint_type_id, product_type_id
                FROM sde_blueprint_products
                WHERE blueprint_type_id IN ({ph})
                  AND activity IN ('manufacturing','reaction')""",
            bp_type_ids,
        ).fetchall()
        product_by_bp: dict[int, int] = {r["blueprint_type_id"]: r["product_type_id"] for r in rows}

        best: dict[int, tuple[int, int]] = {}  # product → (priority, me)
        # priority: BPO = 0 (lepší), BPC = 1; nižší vyhrává
        for bp in blueprints:
            prod = product_by_bp.get(bp.type_id)
            if prod is None:
                continue
            key = (0 if bp.is_original else 1, -bp.material_efficiency)
            prev = best.get(prod)
            if prev is None or key < prev:
                best[prod] = key
                self._bp_me_by_product[prod] = bp.material_efficiency

    def get_type_name(self, type_id: int) -> str:
        row = self.conn.execute(
            "SELECT name FROM sde_types WHERE type_id=?", (type_id,)
        ).fetchone()
        return row["name"] if row else f"Unknown ({type_id})"

    def find_blueprint(self, product_type_id: int) -> sqlite3.Row | None:
        """Najde blueprint, který produkuje daný typ (manufacturing nebo reaction).

        Selection rules — vyřeší případy kde SDE nese víc receptů pro stejný produkt:

        1. Vyřadí blueprinty s "TEST" / "Test " / "QA " / "Tournament" v názvu
           — to jsou tutoriálové / interní CCP blueprinty (např. "Test Reaction
           Blueprint" vyrábí Tungsten Carbide se 500× nižším yieldem než
           pravý recept; bug propagoval do 43 dalších T2 produktů).
        2. Preferuje recept s nejvyšším výstupem na cyklus (`p.quantity DESC`)
           — pravé recepty mívají větší yield než legacy/test verze.
        3. Při tie na yield preferuje vyšší `blueprint_type_id` (novější
           záznam v SDE; CCP občas přejmenuje BP a starý nechá v datech).
        """
        # GLOB is case-sensitive in SQLite (LIKE is not, so 'Protest' would
        # match '%TEST%'). Patterns target only the known CCP-internal BP
        # naming conventions.
        return self.conn.execute("""
            SELECT p.blueprint_type_id, p.quantity AS product_qty, p.activity,
                   b.manufacturing_time, b.reaction_time
            FROM sde_blueprint_products p
            JOIN sde_blueprints b ON b.blueprint_type_id = p.blueprint_type_id
            JOIN sde_types t ON t.type_id = p.blueprint_type_id
            WHERE p.product_type_id = ?
              AND p.activity IN ('manufacturing', 'reaction')
              AND t.name NOT GLOB 'Test *'
              AND t.name NOT GLOB '* TEST *'
              AND t.name NOT GLOB '* TEST Blueprint'
              AND t.name NOT GLOB 'Tournament *'
              AND t.name NOT GLOB 'QA *'
            ORDER BY p.quantity DESC, p.blueprint_type_id DESC
            LIMIT 1
        """, (product_type_id,)).fetchone()

    def get_materials(self, blueprint_type_id: int, activity: str) -> list[sqlite3.Row]:
        return self.conn.execute("""
            SELECT m.material_type_id, m.quantity, t.name
            FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id = ? AND m.activity = ?
        """, (blueprint_type_id, activity)).fetchall()

    def _product_facility_multiplier(
        self,
        product_type_id: int,
        facility: StationFacility,
    ) -> float:
        """Vrátí ME multiplikátor pro konkrétní produkt — filtruje rigy podle
        toho, jestli se aplikují na kategorii produktu (Equipment rig
        se neaplikuje na lodě atd.).
        """
        from app.web.industry_helper import rig_applies_to_product

        multiplier = 1.0 - facility.structure_pct / 100
        for rig_id, me_b, _te_b in facility.rigs:
            if me_b <= 0:
                continue
            if rig_applies_to_product(self.conn, rig_id, product_type_id):
                multiplier *= 1.0 - me_b * facility.sec_multiplier / 100
        return max(0.01, multiplier)

    def resolve(
        self,
        type_id: int,
        quantity: int,
        me: float | None = None,        # Root ME override; None → použít user BP nebo 0
        mfg_facility: StationFacility | None = None,  # Stanice pro výrobní uzly
        rxn_facility: StationFacility | None = None,  # Stanice pro reakční uzly
        depth: int = 0,
        visited: set[int] | None = None,
    ) -> BOMNode:
        """
        Rekurzivně rozloží výrobu daného typu na primární suroviny.

        me: pro root uzel — None znamená použít nejlepší user BP (nebo 0 pokud žádný).
        Pro mezikroky se vždy hledá per-product ME v `_bp_me_by_product`.

        mfg_facility / rxn_facility: konfigurace stanice pro per-product ME multiplikátor.
            Pokud None, ME bonus stanice se neaplikuje (NPC stanice).
        """
        if visited is None:
            visited = set()
        if mfg_facility is None:
            mfg_facility = StationFacility()
        if rxn_facility is None:
            rxn_facility = StationFacility()

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

        # Root override má přednost; jinak per-product lookup (děti nebo neexplicitní root).
        if me is None:
            effective_me = float(self._bp_me_by_product.get(type_id, 0))
        else:
            effective_me = float(me)

        # Per-product facility multiplikátor — uplatní jen rigy aplikovatelné na tento produkt
        facility = mfg_facility if activity == "manufacturing" else rxn_facility
        prod_mult = self._product_facility_multiplier(type_id, facility)

        runs = ceil(quantity / product_qty_per_run)
        materials = self.get_materials(bp_type_id, activity)

        node = BOMNode(
            type_id=type_id, name=name, quantity=quantity,
            runs=runs, is_leaf=False, activity=activity,
            blueprint_type_id=bp_type_id,
            me=int(effective_me),
        )

        visited = visited | {type_id}  # immutable kopie pro větve

        for mat in materials:
            mat_qty = self._apply_me(mat["quantity"], runs, effective_me, prod_mult)
            child = self.resolve(
                type_id=mat["material_type_id"],
                quantity=mat_qty,
                me=None,  # děti používají vlastní per-product ME
                mfg_facility=mfg_facility,
                rxn_facility=rxn_facility,
                depth=depth + 1,
                visited=visited,
            )
            node.children.append(child)

        return node

    @staticmethod
    def _apply_me(base_qty: int, runs: int, me: float, facility_multiplier: float = 1.0) -> int:
        """
        EVE formula (per CCP): max(runs, ceil(round(base * runs * (1-ME/100) * fac_mult, 2)))
        kde fac_mult je už multiplikativně sloučený multiplikátor struktury a rigů
        (např. 0.87 = úspora 13 %). round(..., 2) před ceil zabrání floating-point driftu.
        """
        raw = base_qty * runs * (1 - me / 100) * facility_multiplier
        return max(runs, ceil(round(raw, 2)))
