"""Vizualizace cenového souhrnu pomocí Rich."""
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
        title=f"Náklady na výrobu: {summary.product_name} ×{summary.quantity:,}",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Surovina", style="green", min_width=32)
    table.add_column("Množství", justify="right")
    table.add_column("Cena/ks (ISK)", justify="right", style="cyan")
    table.add_column("Celkem (ISK)", justify="right", style="bold white")

    for mat in sorted(summary.materials, key=lambda m: -(m.total_price or 0)):
        table.add_row(
            mat.name,
            f"{mat.quantity:,}",
            _isk(mat.unit_price),
            _isk(mat.total_price),
        )

    # Součet
    total = summary.total_material_cost
    table.add_section()
    table.add_row(
        "[bold]CELKEM MATERIÁLY[/]", "", "",
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

    lines.append(f"  Výroba {summary.quantity}× [cyan]{summary.product_name}[/]")
    lines.append("")
    lines.append(f"  Náklady na materiály : [bold]{_isk(total_cost)} ISK[/]")
    lines.append(f"  Jita sell (prodat)   : [bold]{_isk(summary.product_sell_price)} ISK/ks[/]  →  příjem [bold]{_isk(sell_revenue)} ISK[/]")
    lines.append(f"  Jita buy (koupit hot): [bold]{_isk(summary.product_buy_price)} ISK/ks[/]  →  cena [bold]{_isk(buy_cost)} ISK[/]")
    lines.append("")

    if profit_sell is not None:
        color = "green" if profit_sell >= 0 else "red"
        sign  = "+" if profit_sell >= 0 else ""
        lines.append(f"  Zisk (výroba → prodat)  : [{color}]{sign}{_isk(profit_sell)} ISK[/]")

    if margin is not None:
        color = "green" if margin >= 0 else "red"
        lines.append(f"  Marže                  : [{color}]{margin:+.1f}%[/]")

    if profit_buy is not None:
        color = "green" if profit_buy >= 0 else "red"
        sign  = "+" if profit_buy >= 0 else ""
        lines.append(f"  Úspora vs. koupit hotové: [{color}]{sign}{_isk(profit_buy)} ISK[/]")

    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Cenový souhrn[/]", border_style="yellow"))


def print_optimization(result: OptimizationResult, product_name: str, quantity: int, product_sell_price: float | None):
    """Zobrazí make vs. buy rozhodnutí a optimalizovaný cenový souhrn."""

    buy_dec  = result.buy_decisions
    make_dec = result.make_decisions

    # --- Tabulka: co KOUPIT (seřazeno podle úspory) ---
    if buy_dec:
        t_buy = Table(
            title=f"[green]KOUPIT na Jitě[/] ({len(buy_dec)} komponentů)",
            box=box.ROUNDED, show_lines=True,
        )
        t_buy.add_column("Komponent",       style="white",  min_width=34)
        t_buy.add_column("Množství",        justify="right")
        t_buy.add_column("Nákup (ISK)",     justify="right", style="green")
        t_buy.add_column("Výroba by stála", justify="right", style="dim")
        t_buy.add_column("Úspora (ISK)",    justify="right", style="bold green")

        for d in sorted(buy_dec, key=lambda d: (d.savings or 0)):   # nejmenší savings = nejlevnější koupit
            saved = -(d.savings or 0)   # savings je záporné pro "koupit je levnější"
            t_buy.add_row(
                d.name,
                f"{d.quantity:,}",
                _isk(d.buy_cost),
                _isk(d.make_cost),
                _isk(saved),
            )
        console.print()
        console.print(t_buy)

    # --- Tabulka: co VYROBIT ---
    if make_dec:
        t_make = Table(
            title=f"[cyan]VYROBIT[/] ({len(make_dec)} komponentů)",
            box=box.ROUNDED, show_lines=True,
        )
        t_make.add_column("Komponent",       style="white",  min_width=34)
        t_make.add_column("Množství",        justify="right")
        t_make.add_column("Výroba (ISK)",    justify="right", style="cyan")
        t_make.add_column("Nákup by stál",   justify="right", style="dim")
        t_make.add_column("Úspora výrobou",  justify="right", style="bold cyan")

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

    # --- Souhrn ---
    sell_revenue = (product_sell_price or 0) * quantity
    opt_profit   = (sell_revenue - result.total_cost)  if result.total_cost  is not None else None
    naive_profit = (sell_revenue - result.naive_cost)  if result.naive_cost  is not None else None

    lines = [
        f"  Výroba {quantity}× [cyan]{product_name}[/]",
        "",
        f"  Naivní cena (vše vyrobit) : [bold]{_isk(result.naive_cost)} ISK[/]",
        f"  Optimální cena            : [bold yellow]{_isk(result.total_cost)} ISK[/]",
    ]

    if (result.total_savings or 0) > 0:
        lines.append(f"  Celková úspora            : [bold green]+{_isk(result.total_savings)} ISK[/]")
    else:
        lines.append(f"  Celková úspora            : [dim]{_isk(result.total_savings)} ISK[/]")

    lines += [
        "",
        f"  Rozhodnutí: [green]{len(buy_dec)}× KOUPIT[/]  |  [cyan]{len(make_dec)}× VYROBIT[/]",
    ]

    if product_sell_price is not None:
        lines += [
            "",
            f"  Jita sell cena produktu   : [bold]{_isk(product_sell_price)} ISK/ks[/]",
            f"  Celkový příjem            : [bold]{_isk(sell_revenue)} ISK[/]",
        ]
        if naive_profit is not None:
            color = "green" if naive_profit >= 0 else "red"
            sign  = "+" if naive_profit >= 0 else ""
            lines.append(f"  Zisk bez optimalizace     : [{color}]{sign}{_isk(naive_profit)} ISK[/]")
        if opt_profit is not None:
            color = "green" if opt_profit >= 0 else "red"
            sign  = "+" if opt_profit >= 0 else ""
            lines.append(f"  Zisk s optimalizací       : [{color}]{sign}{_isk(opt_profit)} ISK[/]")

    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Optimalizovaný cenový souhrn[/]", border_style="yellow"))


def print_top_costs(summary: BOMCostSummary, top_n: int = 10):
    """Zobrazí top N nejdražších surovin."""
    ranked = sorted(
        [m for m in summary.materials if m.total_price],
        key=lambda m: -(m.total_price or 0)
    )[:top_n]

    total = summary.total_material_cost or 1
    table = Table(title=f"Top {top_n} nejdražších surovin", box=box.SIMPLE)
    table.add_column("#", style="dim", width=3)
    table.add_column("Surovina", style="green")
    table.add_column("Celkem ISK", justify="right", style="bold white")
    table.add_column("% z celku", justify="right", style="cyan")

    for i, mat in enumerate(ranked, 1):
        pct = (mat.total_price / total * 100) if mat.total_price else 0
        table.add_row(str(i), mat.name, _isk(mat.total_price), f"{pct:.1f}%")

    console.print()
    console.print(table)
