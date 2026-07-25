"""
Compute manufacturing costs from the BOM tree and market prices.
"""
from __future__ import annotations
from dataclasses import dataclass
from app.bom.resolver import BOMNode


@dataclass
class MaterialCost:
    type_id: int
    name: str
    quantity: int
    unit_price: float | None    # ISK per unit
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
    product_sell_price: float | None    # what we get from selling the finished product
    product_buy_price: float | None     # what we pay if we buy the finished product

    @property
    def total_material_cost(self) -> float | None:
        totals = [m.total_price for m in self.materials if m.total_price is not None]
        return sum(totals) if totals else None

    @property
    def profit_vs_buy(self) -> float | None:
        """Savings versus buying the finished product (positive = making is cheaper)."""
        if self.product_buy_price is None or self.total_material_cost is None:
            return None
        return (self.product_buy_price * self.quantity) - self.total_material_cost

    @property
    def profit_vs_sell(self) -> float | None:
        """Profit after selling the manufactured product (positive = making is worth it)."""
        if self.product_sell_price is None or self.total_material_cost is None:
            return None
        return (self.product_sell_price * self.quantity) - self.total_material_cost

    @property
    def margin_pct(self) -> float | None:
        """Margin in % (profit / cost * 100)."""
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
    Build a price summary from the BOM tree and a price dictionary.
    prices: output of fetch_jita_prices_bulk or adjusted prices
    """
    leaves = root.aggregate_leaves()

    materials = []
    for type_id, (name, qty) in sorted(leaves.items(), key=lambda x: x[1][0]):
        sell_p, _ = prices.get(type_id, (None, None))
        unit = sell_p  # we buy materials → we pay the sell price
        total = unit * qty if unit is not None else None
        materials.append(MaterialCost(type_id, name, qty, unit, total))

    # Price of the finished product
    prod_sell, prod_buy = prices.get(root.type_id, (None, None))

    return BOMCostSummary(
        product_type_id=root.type_id,
        product_name=root.name,
        quantity=root.quantity,
        materials=materials,
        product_sell_price=prod_sell,
        product_buy_price=prod_buy,
    )
