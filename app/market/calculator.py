"""
Výpočet nákladů výroby z BOM stromu a tržních cen.
"""
from __future__ import annotations
from dataclasses import dataclass
from app.bom.resolver import BOMNode


@dataclass
class MaterialCost:
    type_id: int
    name: str
    quantity: int
    unit_price: float | None    # ISK za kus
    total_price: float | None   # unit_price * quantity

    @property
    def formatted_total(self) -> str:
        if self.total_price is None:
            return "N/A"
        return f"{self.total_price:,.0f}"

    @property
    def formatted_unit(self) -> str:
        if self.unit_price is None:
            return "N/A"
        return f"{self.unit_price:,.2f}"


@dataclass
class BOMCostSummary:
    product_type_id: int
    product_name: str
    quantity: int
    materials: list[MaterialCost]
    product_sell_price: float | None    # co dostaneme prodejem hotového produktu
    product_buy_price: float | None     # co zaplatíme kdybychom koupili hotový produkt

    @property
    def total_material_cost(self) -> float | None:
        totals = [m.total_price for m in self.materials if m.total_price is not None]
        return sum(totals) if totals else None

    @property
    def profit_vs_buy(self) -> float | None:
        """Úspora oproti nákupu hotového produktu (kladné = výroba je levnější)."""
        if self.product_buy_price is None or self.total_material_cost is None:
            return None
        return (self.product_buy_price * self.quantity) - self.total_material_cost

    @property
    def profit_vs_sell(self) -> float | None:
        """Zisk po prodeji vyrobeného produktu (kladné = výroba se vyplatí)."""
        if self.product_sell_price is None or self.total_material_cost is None:
            return None
        return (self.product_sell_price * self.quantity) - self.total_material_cost

    @property
    def margin_pct(self) -> float | None:
        """Marže v % (zisk / náklady * 100)."""
        p = self.profit_vs_sell
        c = self.total_material_cost
        if p is None or c is None or c == 0:
            return None
        return (p / c) * 100


def build_cost_summary(
    root: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],  # {type_id: (sell, buy)}
) -> BOMCostSummary:
    """
    Sestaví cenový souhrn z BOM stromu a slovníku cen.
    prices: výstup fetch_jita_prices_bulk nebo adjusted prices
    """
    leaves = root.aggregate_leaves()

    materials = []
    for type_id, (name, qty) in sorted(leaves.items(), key=lambda x: x[1][0]):
        sell_p, _ = prices.get(type_id, (None, None))
        unit = sell_p  # nakupujeme materiály → platíme sell cenu
        total = unit * qty if unit is not None else None
        materials.append(MaterialCost(type_id, name, qty, unit, total))

    # Cena hotového produktu
    prod_sell, prod_buy = prices.get(root.type_id, (None, None))

    return BOMCostSummary(
        product_type_id=root.type_id,
        product_name=root.name,
        quantity=root.quantity,
        materials=materials,
        product_sell_price=prod_sell,
        product_buy_price=prod_buy,
    )
