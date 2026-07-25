"""
EVE Retroindustry Calculator
Usage:
  python main.py <name>                            -- search for a type by name
  python main.py <type_id>                         -- direct type_id
  python main.py <name> --qty 5                    -- number of units (default 1)
  python main.py <name> --me 10                    -- Material Efficiency 0-10 (default 0)
  python main.py <name> --no-tree                  -- do not show the production tree
  python main.py <name> --jita                     -- live Jita prices (30 min cache)
  python main.py <name> --jita --optimize          -- make vs. buy optimization
"""
import asyncio
import sys
import argparse
import os
import sqlite3
import httpx
from rich.console import Console

from app.db.database import get_session
from app.cache.blueprint_cache import resolve_type
from app.esi.client import search_type_by_name
from app.bom.resolver import BOMResolver
from app.bom.optimizer import optimize
from app.bom.display import print_bom_tree, print_primary_materials, print_bom_stats
from app.market.prices import (
    fetch_adjusted_prices,
    fetch_jita_prices_bulk,
    ensure_price_table,
)
from app.market.calculator import build_cost_summary
from app.market.display import (
    print_cost_table,
    print_profit_summary,
    print_top_costs,
    print_optimization,
)

console = Console()
DB_ABS = os.path.abspath(os.path.join(os.path.dirname(__file__), "eve_cache.db"))


async def find_type_id(query: str) -> tuple[int, str] | None:
    async with httpx.AsyncClient() as client:
        session = get_session()
        console.print(f"[dim]Searching for '{query}'...[/]")
        results = await search_type_by_name(client, query)
        if not results:
            console.print(f"[red]Nothing found for '{query}'[/]")
            return None

        top = results[:8]
        names = await asyncio.gather(*[resolve_type(client, session, tid) for tid in top])
        session.close()

        for tid, name in zip(top, names):
            if name.lower() == query.lower():
                return tid, name

        console.print(f"\n[bold]Types found:[/]")
        for i, (tid, name) in enumerate(zip(top, names)):
            console.print(f"  [{i+1}] {name}  [dim](ID: {tid})[/]")

        choice = console.input("\n[bold]Select a number (or Enter for the first): [/]").strip()
        idx = (int(choice) - 1) if choice.isdigit() else 0
        return top[idx], names[idx]


async def get_prices(
    type_ids: list[int],
    use_jita: bool,
) -> dict[int, tuple[float | None, float | None]]:
    """Return {type_id: (sell_price, buy_price)}."""
    async with httpx.AsyncClient() as client:
        if use_jita:
            console.print(f"[dim]Loading live Jita prices for {len(type_ids)} types...[/]")
            conn = sqlite3.connect(DB_ABS)
            ensure_price_table(conn)
            result = await fetch_jita_prices_bulk(client, conn, type_ids)
            conn.close()
            return result
        else:
            console.print("[dim]Loading adjusted prices (ESI)...[/]")
            adj = await fetch_adjusted_prices(client)
            return {
                tid: (
                    adj[tid].get("average_price") if tid in adj else None,
                    adj[tid].get("average_price") if tid in adj else None,
                )
                for tid in type_ids
            }


def collect_all_type_ids(root) -> list[int]:
    """Collect type_ids of all nodes (leaf and non-leaf) for price loading."""
    ids: set[int] = set()

    def walk(node):
        ids.add(node.type_id)
        for c in node.children:
            walk(c)

    walk(root)
    return list(ids)


def parse_args():
    parser = argparse.ArgumentParser(description="EVE Retroindustry BOM Calculator")
    parser.add_argument("query",        help="Type name or type_id")
    parser.add_argument("--qty",        type=int,   default=1,    help="Number of units (default: 1)")
    parser.add_argument("--me",         type=float, default=0.0,  help="Material Efficiency 0-10 (default: 0)")
    parser.add_argument("--no-tree",    action="store_true",       help="Do not show the production tree")
    parser.add_argument("--jita",       action="store_true",       help="Live Jita sell/buy prices")
    parser.add_argument("--optimize",   action="store_true",       help="Make vs. Buy optimization (requires --jita)")
    return parser.parse_args()


async def main():
    args = parse_args()

    if args.optimize and not args.jita:
        console.print("[yellow]Note: --optimize requires live prices, enabling --jita automatically[/]")
        args.jita = True

    if args.query.isdigit():
        type_id = int(args.query)
        async with httpx.AsyncClient() as client:
            session = get_session()
            type_name = await resolve_type(client, session, type_id)
            session.close()
    else:
        result = await find_type_id(args.query)
        if not result:
            sys.exit(1)
        type_id, type_name = result

    price_mode = "Jita live" if args.jita else "ESI adjusted avg"
    console.print(f"\n[bold]EVE Retroindustry BOM Calculator[/]")
    console.print(f"  Product   : [cyan]{type_name}[/] (ID: {type_id})")
    console.print(f"  Quantity  : {args.qty:,}")
    console.print(f"  ME        : {args.me:.0f}")
    console.print(f"  Prices    : {price_mode}")
    if args.optimize:
        console.print(f"  Mode      : [bold green]Make vs. Buy optimization[/]")

    # BOM tree
    resolver = BOMResolver(DB_ABS)
    root = resolver.resolve(type_id, args.qty, me=args.me)
    resolver.close()

    if root.is_leaf:
        console.print(f"\n[yellow]'{type_name}' has no production blueprint — it is a raw material.[/]")
        return

    # Production tree
    if not args.no_tree:
        print_bom_tree(root)

    if args.optimize:
        # Load prices for ALL nodes (not just leaves) — we also need intermediates
        all_ids = collect_all_type_ids(root)
        prices = await get_prices(all_ids, use_jita=True)

        opt_result = optimize(root, prices)
        sell_p, _ = prices.get(type_id, (None, None))
        print_optimization(opt_result, type_name, args.qty, sell_p)
    else:
        # Standard mode — raw materials + prices
        print_primary_materials(root)
        print_bom_stats(root)

        leaves = root.aggregate_leaves()
        all_type_ids = list(leaves.keys()) + [type_id]
        prices = await get_prices(all_type_ids, use_jita=args.jita)

        summary = build_cost_summary(root, prices)
        print_cost_table(summary)
        print_top_costs(summary, top_n=10)
        print_profit_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())
