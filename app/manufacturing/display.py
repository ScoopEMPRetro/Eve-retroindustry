"""Vizualizace výrobního plánu."""
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from app.manufacturing.planner import ManufacturingPlan

console = Console()


def _isk(v: float | None) -> str:
    if v is None:
        return "[dim]N/A[/]"
    return f"{v:,.2f}".replace(",", " ")


def print_plan(
    plan: ManufacturingPlan,
    location_name: str = "",
    prices: dict[int, tuple[float | None, float | None]] | None = None,
):
    loc_label = location_name or str(plan.location_id)
    price_mode = prices is not None

    # --- Záhlaví ---
    bp = plan.blueprint
    if bp:
        bp_kind = "[green]BPO (originál)[/]" if bp.is_original else "[yellow]BPC (kopie)[/]"
        bp_runs = "∞" if bp.runs == -1 else str(bp.runs)
        bp_info = f"{bp_kind}  ME:{plan.me}  TE:{plan.te}  Runy: {bp_runs}"
    else:
        bp_info = "[red]Postava nemá blueprint pro tento produkt[/]"

    mode_labels = {
        "full":       "[white]full[/]       — základní suroviny (celý výrobní řetězec)",
        "components": "[yellow]components[/] — přímé komponenty 1. úrovně (koupit hotové)",
        "optimal":    "[green]optimal[/]    — make vs. buy optimalizace",
    }
    mode_str = mode_labels.get(plan.mode, plan.mode)

    header_lines = [
        f"  Produkt  : [cyan]{plan.product_name}[/] ×{plan.quantity:,}",
        f"  Blueprint: {bp_info}",
        f"  Stanice  : [bold]{loc_label}[/]  (ID: {plan.location_id})",
        f"  Mód      : {mode_str}",
        "",
    ]

    if plan.mode == "optimal" and plan.opt_total_cost is not None:
        header_lines.append(
            f"  Náklady  : [bold yellow]{_isk(plan.opt_total_cost)} ISK[/]"
            f"  [dim](naivní: {_isk(plan.opt_naive_cost)} ISK)[/]"
        )

    if plan.can_manufacture:
        header_lines.append("  [bold green]✓ Máš dostatek materiálů k výrobě![/]")
    else:
        header_lines.append(
            f"  [bold red]✗ Chybí {plan.total_missing_types} druhů materiálů[/]"
        )

    console.print()
    console.print(Panel("\n".join(header_lines), title="[bold]Výrobní plán[/]", border_style="cyan"))

    # --- Tabulka materiálů ---
    table = Table(title="Materiály", box=box.ROUNDED, show_lines=True)
    table.add_column("Surovina",          style="white", min_width=32)
    table.add_column("Potřeba",           justify="right")
    if price_mode:
        table.add_column("Cena/ks (ISK)",     justify="right", style="cyan")
        table.add_column("Celkem (ISK)",      justify="right", style="cyan")
    table.add_column("Na stanici",        justify="right")
    table.add_column("Chybí",             justify="right")
    if price_mode:
        table.add_column("Dokoupit (ISK)",    justify="right", style="bold yellow")
    table.add_column("Krytí",             justify="right")
    table.add_column("Stav",              justify="center", width=4)

    sorted_mats = sorted(plan.materials, key=lambda m: (m.ok, m.coverage_pct))

    for m in sorted_mats:
        cov_color   = "green" if m.ok else ("yellow" if m.coverage_pct >= 50 else "red")
        status      = "[green]✓[/]" if m.ok else "[red]✗[/]"
        missing_str = f"[red]{m.missing:,}[/]" if m.missing > 0 else "[dim]—[/]"

        row = [m.name, f"{m.required:,}"]

        if price_mode:
            sell_p, _ = prices.get(m.type_id, (None, None))
            unit_str  = _isk(sell_p)
            total_str = _isk(sell_p * m.required) if sell_p is not None else "[dim]N/A[/]"
            row += [unit_str, total_str]

        row += [f"{m.available:,}", missing_str]

        if price_mode:
            sell_p, _ = prices.get(m.type_id, (None, None))
            if m.missing > 0 and sell_p is not None:
                buy_str = _isk(sell_p * m.missing)
            elif m.missing == 0:
                buy_str = "[dim]—[/]"
            else:
                buy_str = "[dim]N/A[/]"
            row.append(buy_str)

        row += [f"[{cov_color}]{m.coverage_pct:.0f}%[/]", status]
        table.add_row(*row)

    console.print()
    console.print(table)

    # --- Nákupní seznam s cenami ---
    missing_mats = [m for m in plan.materials if not m.ok]
    if not missing_mats:
        return

    if price_mode:
        _print_shopping_bill(missing_mats, prices, plan)
    else:
        console.print(f"\n[bold red]Chybějící materiály k dokoupení ({len(missing_mats)}):[/]")
        buy_table = Table(box=box.SIMPLE)
        buy_table.add_column("Surovina", style="red", min_width=32)
        buy_table.add_column("Dokoupit", justify="right", style="bold red")
        buy_table.add_column("Type ID",  justify="right", style="dim")
        for m in sorted(missing_mats, key=lambda m: -m.missing):
            buy_table.add_row(m.name, f"{m.missing:,}", str(m.type_id))
        console.print(buy_table)


def _print_shopping_bill(
    missing_mats,
    prices: dict[int, tuple[float | None, float | None]],
    plan: ManufacturingPlan,
):
    """Nákupní účet — chybějící materiály seřazené podle ceny."""
    rows = []
    total_known = 0.0
    missing_price_count = 0

    for m in missing_mats:
        sell_p, _ = prices.get(m.type_id, (None, None))
        if sell_p is not None:
            cost = sell_p * m.missing
            total_known += cost
            rows.append((m.name, m.missing, sell_p, cost))
        else:
            missing_price_count += 1
            rows.append((m.name, m.missing, None, None))

    # Seřadit podle ceny DESC
    rows.sort(key=lambda r: -(r[3] or 0))

    table = Table(
        title=f"Nákupní seznam — {plan.product_name} ×{plan.quantity:,}",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Surovina",        style="white", min_width=32)
    table.add_column("Dokoupit",        justify="right")
    table.add_column("Cena/ks (ISK)",   justify="right", style="cyan")
    table.add_column("Celkem (ISK)",    justify="right", style="bold yellow")

    for name, qty, unit, total in rows:
        table.add_row(
            name,
            f"{qty:,}",
            _isk(unit),
            _isk(total),
        )

    # Součtový řádek
    table.add_section()
    table.add_row(
        f"[bold]CELKEM[/] [dim]({len(rows)} položek)[/]",
        "",
        "",
        f"[bold green]{_isk(total_known)}[/]",
    )
    if missing_price_count:
        table.add_row(
            f"[dim]+ {missing_price_count} položek bez ceny[/]",
            "", "", "",
        )

    console.print()
    console.print(table)

    # Panel souhrn
    sell_p, _ = prices.get(plan.product_type_id, (None, None))
    lines = [
        f"  Produkt      : [cyan]{plan.product_name}[/] ×{plan.quantity:,}",
        f"  Nákup surovin: [bold yellow]{_isk(total_known)} ISK[/]",
    ]
    if sell_p:
        revenue = sell_p * plan.quantity
        profit  = revenue - total_known
        color   = "green" if profit >= 0 else "red"
        sign    = "+" if profit >= 0 else ""
        lines += [
            f"  Jita sell    : [bold]{_isk(sell_p)} ISK/ks[/]  →  příjem [bold]{_isk(revenue)} ISK[/]",
            f"  Zisk         : [{color}]{sign}{_isk(profit)} ISK[/]",
        ]
    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Finanční souhrn[/]", border_style="yellow"))
