"""
Make vs. Buy optimalizátor.

Algoritmus (bottom-up):
  Pro každý uzel stromu spočítáme optimální cenu:
  - leaf:     sell_price × quantity
  - non-leaf: min(make_cost, buy_cost)
              make_cost = součet optimálních cen potomků
              buy_cost  = Jita sell_price × quantity

  Výsledkem je minimální celková cena a seznam rozhodnutí pro každý meziprodukt.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
from app.bom.resolver import BOMNode


@dataclass
class Decision:
    type_id: int
    name: str
    quantity: int
    make_cost: float | None     # cena výroby (s optimálními dětmi)
    buy_cost: float | None      # Jita sell cena × qty
    action: Literal["make", "buy", "unknown"]
    savings: float | None       # kladné = výroba levnější, záporné = nákup levnější


@dataclass
class OptimizationResult:
    total_cost: float | None
    naive_cost: float | None            # bez optimalizace (vše vyrábět)
    total_savings: float | None         # naive - optimized
    decisions: list[Decision] = field(default_factory=list)

    @property
    def buy_decisions(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "buy"]

    @property
    def make_decisions(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "make"]


def optimize(
    root: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
) -> OptimizationResult:
    """
    Spustí make vs. buy optimalizaci na BOM stromu.
    prices: {type_id: (sell_price, buy_price)}
    """
    raw_decisions: list[Decision] = []
    opt_cost   = _optimize_node(root, prices, raw_decisions, is_root=True)
    naive_cost = _naive_cost(root, prices)

    # Deduplikace: agregujeme přes type_id (stejný komponent v různých větvích)
    merged: dict[int, Decision] = {}
    for d in raw_decisions:
        if d.type_id in merged:
            ex = merged[d.type_id]
            new_qty  = ex.quantity  + d.quantity
            new_make = (ex.make_cost or 0) + (d.make_cost or 0) if (ex.make_cost is not None or d.make_cost is not None) else None
            new_buy  = (ex.buy_cost  or 0) + (d.buy_cost  or 0) if (ex.buy_cost  is not None or d.buy_cost  is not None) else None
            new_sav  = (new_make - new_buy) if (new_make is not None and new_buy is not None) else None
            new_act: Literal["make", "buy", "unknown"] = (
                "buy" if (new_buy is not None and new_make is not None and new_buy < new_make)
                else "make" if new_make is not None
                else "buy" if new_buy is not None
                else "unknown"
            )
            merged[d.type_id] = Decision(d.type_id, d.name, new_qty, new_make, new_buy, new_act, new_sav)
        else:
            merged[d.type_id] = d

    decisions = sorted(merged.values(), key=lambda d: -(abs(d.savings or 0)))
    savings = (naive_cost - opt_cost) if (naive_cost is not None and opt_cost is not None) else None

    return OptimizationResult(
        total_cost=opt_cost,
        naive_cost=naive_cost,
        total_savings=savings,
        decisions=decisions,
    )


def _optimize_node(
    node: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
    decisions: list[Decision],
    is_root: bool = False,
) -> float | None:
    sell_p, _ = prices.get(node.type_id, (None, None))

    if node.is_leaf:
        return (sell_p * node.quantity) if sell_p is not None else None

    # Rekurzivně spočítáme optimální cenu výroby (potomci už optimalizovaní)
    children_costs = [
        _optimize_node(child, prices, decisions)
        for child in node.children
    ]
    if any(c is None for c in children_costs):
        make_cost = None
    else:
        make_cost = sum(children_costs)

    buy_cost = (sell_p * node.quantity) if sell_p is not None else None

    # Rozhodnutí
    if make_cost is not None and buy_cost is not None:
        action: Literal["make", "buy", "unknown"] = "buy" if buy_cost < make_cost else "make"
        savings = make_cost - buy_cost   # kladné = výroba levnější, záporné = koupit levnější
    elif buy_cost is None:
        action = "make"
        savings = None
    else:
        action = "buy"
        savings = None

    # Kořen (samotný produkt) do decisions nepřidáváme
    if not is_root:
        decisions.append(Decision(
            type_id=node.type_id,
            name=node.name,
            quantity=node.quantity,
            make_cost=make_cost,
            buy_cost=buy_cost,
            action=action,
            savings=savings,
        ))

    if action == "buy" and buy_cost is not None:
        return buy_cost
    return make_cost


def get_shopping_list(
    root: BOMNode,
    decisions: dict[int, Decision],
) -> dict[int, tuple[str, int]]:
    """
    Z výsledků optimalizace sestaví nákupní seznam.
    Pro každý uzel:
      - "buy" rozhodnutí → přidej komponent do nákupního seznamu, nesestupuj hlouběji
      - list (primární surovina) → přidej do nákupního seznamu
      - "make" → pokračuj do potomků
    Vrací {type_id: (name, total_quantity)}.
    """
    shopping: dict[int, tuple[str, int]] = {}

    def _add(type_id: int, name: str, qty: int):
        prev_qty = shopping.get(type_id, (name, 0))[1]
        shopping[type_id] = (name, prev_qty + qty)

    def traverse(node: BOMNode, is_root: bool = False):
        if node.is_leaf:
            _add(node.type_id, node.name, node.quantity)
            return

        decision = decisions.get(node.type_id)
        if not is_root and decision and decision.action == "buy":
            _add(node.type_id, node.name, node.quantity)
            return  # neklesáme hlouběji — kupujeme hotový komponent

        for child in node.children:
            traverse(child)

    traverse(root, is_root=True)
    return shopping


def _naive_cost(
    node: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
) -> float | None:
    """Naivní cena — vše vyrábíme, listy kupujeme."""
    if node.is_leaf:
        sell_p, _ = prices.get(node.type_id, (None, None))
        return (sell_p * node.quantity) if sell_p is not None else None

    child_costs = [_naive_cost(c, prices) for c in node.children]
    if any(c is None for c in child_costs):
        return None
    return sum(child_costs)
