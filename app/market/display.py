"""Price summary visualization using Rich."""
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from app.market.calculator import BOMCostSummary
from app.bom.optimizer import OptimizationResult

console = Console()


def _isk(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "[dim]N/A[/]"
    return f"{value:,.2f}{suffix}".replace(",", " ")


def print_cost_table(summary: BOMCostSummary):
    table = Table(
        title=f"Manufacturing cost: {summary.product_name} ×{summary.quantity:,}",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Material", style="green", min_width=32)
    table.add_column("Quantity", justify="right")
    table.add_column("Price/unit (ISK)", justify="right", style="cyan")
    table.add_column("Total (ISK)", justify="right", style="bold white")

    for mat in sorted(summary.materials, key=lambda m: -(m.total_price or 0)):
        table.add_row(
            mat.name,
            f"{mat.quantity:,}",
            _isk(mat.unit_price),
            _isk(mat.total_price),
        )

    # Total
    total = summary.total_material_cost
    table.add_section()
    table.add_row(
        "[bold]TOTAL MATERIALS[/]", "", "",
        f"[bold yellow]{_isk(total)}[/]" if total else "[dim]N/A[/]"
    )

    console.print()
    console.print(table)


def print_profit_summary(summary: BOMCostSummary):
    total_cost = summary.total_material_cost
    sell_revenue = (summary.product_sell_price or 0) * summary.quantity
    buy_cost     = (summary.product_buy_price  or 0) * summary.quantity
    profit_sell  = summary.profit_vs_sell
    profit_buy   = summary.profit_vs_buy
    margin       = summary.margin_pct

    lines: list[str] = []

    lines.append(f"  Manufacture {summary.quantity}× [cyan]{summary.product_name}[/]")
    lines.append("")
    lines.append(f"  Material cost        : [bold]{_isk(total_cost)} ISK[/]")
    lines.append(f"  Jita sell (sell)     : [bold]{_isk(summary.product_sell_price)} ISK/unit[/]  →  revenue [bold]{_isk(sell_revenue)} ISK[/]")
    lines.append(f"  Jita buy (buy ready) : [bold]{_isk(summary.product_buy_price)} ISK/unit[/]  →  cost [bold]{_isk(buy_cost)} ISK[/]")
    lines.append("")

    if profit_sell is not None:
        color = "green" if profit_sell >= 0 else "red"
        sign  = "+" if profit_sell >= 0 else ""
        lines.append(f"  Profit (make → sell)    : [{color}]{sign}{_isk(profit_sell)} ISK[/]")

    if margin is not None:
        color = "green" if margin >= 0 else "red"
        lines.append(f"  Margin                 : [{color}]{margin:+.1f}%[/]")

    if profit_buy is not None:
        color = "green" if profit_buy >= 0 else "red"
        sign  = "+" if profit_buy >= 0 else ""
        lines.append(f"  Savings vs. buy ready   : [{color}]{sign}{_isk(profit_buy)} ISK[/]")

    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Price summary[/]", border_style="yellow"))


def print_optimization(result: OptimizationResult, product_name: str, quantity: int, product_sell_price: float | None):
    """Show the make vs. buy decision and the optimized price summary."""

    buy_dec  = result.buy_decisions
    make_dec = result.make_decisions

    # --- Table: what to BUY (sorted by savings) ---
    if buy_dec:
        t_buy = Table(
            title=f"[green]BUY on Jita[/] ({len(buy_dec)} components)",
            box=box.ROUNDED, show_lines=True,
        )
        t_buy.add_column("Component",       style="white",  min_width=34)
        t_buy.add_column("Quantity",        justify="right")
        t_buy.add_column("Buy (ISK)",       justify="right", style="green")
        t_buy.add_column("Making would cost", justify="right", style="dim")
        t_buy.add_column("Savings (ISK)",   justify="right", style="bold green")

        for d in sorted(buy_dec, key=lambda d: (d.savings or 0)):   # lowest savings = cheapest to buy
            saved = -(d.savings or 0)   # savings is negative when "buying is cheaper"
            t_buy.add_row(
                d.name,
                f"{d.quantity:,}",
                _isk(d.buy_cost),
                _isk(d.make_cost),
                _isk(saved),
            )
        console.print()
        console.print(t_buy)

    # --- Table: what to MAKE ---
    if make_dec:
        t_make = Table(
            title=f"[cyan]MAKE[/] ({len(make_dec)} components)",
            box=box.ROUNDED, show_lines=True,
        )
        t_make.add_column("Component",       style="white",  min_width=34)
        t_make.add_column("Quantity",        justify="right")
        t_make.add_column("Make (ISK)",      justify="right", style="cyan")
        t_make.add_column("Buying would cost", justify="right", style="dim")
        t_make.add_column("Savings by making", justify="right", style="bold cyan")

        for d in sorted(make_dec, key=lambda d: -(d.savings or 0)):
            t_make.add_row(
                d.name,
                f"{d.quantity:,}",
                _isk(d.make_cost),
                _isk(d.buy_cost),
                _isk(d.savings),
            )
        console.print()
        console.print(t_make)

    # --- Summary ---
    sell_revenue = (product_sell_price or 0) * quantity
    opt_profit   = (sell_revenue - result.total_cost)  if result.total_cost  is not None else None
    naive_profit = (sell_revenue - result.naive_cost)  if result.naive_cost  is not None else None

    lines = [
        f"  Manufacture {quantity}× [cyan]{product_name}[/]",
        "",
        f"  Naive cost (make everything) : [bold]{_isk(result.naive_cost)} ISK[/]",
        f"  Optimal cost                 : [bold yellow]{_isk(result.total_cost)} ISK[/]",
    ]

    if (result.total_savings or 0) > 0:
        lines.append(f"  Total savings                : [bold green]+{_isk(result.total_savings)} ISK[/]")
    else:
        lines.append(f"  Total savings                : [dim]{_isk(result.total_savings)} ISK[/]")

    lines += [
        "",
        f"  Decision: [green]{len(buy_dec)}× BUY[/]  |  [cyan]{len(make_dec)}× MAKE[/]",
    ]

    if product_sell_price is not None:
        lines += [
            "",
            f"  Jita sell price of product   : [bold]{_isk(product_sell_price)} ISK/unit[/]",
            f"  Total revenue                : [bold]{_isk(sell_revenue)} ISK[/]",
        ]
        if naive_profit is not None:
            color = "green" if naive_profit >= 0 else "red"
            sign  = "+" if naive_profit >= 0 else ""
            lines.append(f"  Profit without optimization  : [{color}]{sign}{_isk(naive_profit)} ISK[/]")
        if opt_profit is not None:
            color = "green" if opt_profit >= 0 else "red"
            sign  = "+" if opt_profit >= 0 else ""
            lines.append(f"  Profit with optimization     : [{color}]{sign}{_isk(opt_profit)} ISK[/]")

    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Optimized price summary[/]", border_style="yellow"))


def print_top_costs(summary: BOMCostSummary, top_n: int = 10):
    """Show the top N most expensive materials."""
    ranked = sorted(
        [m for m in summary.materials if m.total_price],
        key=lambda m: -(m.total_price or 0)
    )[:top_n]

    total = summary.total_material_cost or 1
    table = Table(title=f"Top {top_n} most expensive materials", box=box.SIMPLE)
    table.add_column("#", style="dim", width=3)
    table.add_column("Material", style="green")
    table.add_column("Total ISK", justify="right", style="bold white")
    table.add_column("% of total", justify="right", style="cyan")

    for i, mat in enumerate(ranked, 1):
        pct = (mat.total_price / total * 100) if mat.total_price else 0
        table.add_row(str(i), mat.name, _isk(mat.total_price), f"{pct:.1f}%")

    console.print()
    console.print(table)
