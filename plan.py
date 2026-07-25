"""
EVE Retroindustry — production planner using character data.

Usage:
  python plan.py --product "Phoenix" --station 60003760
  python plan.py --product "Phoenix" --station 60003760 --jita
  python plan.py --product 19726 --station 60003760 --qty 2
  python plan.py --list-blueprints
  python plan.py --list-locations
  python plan.py --refresh

Adjusted prices (1 API call) are always shown.
--jita         Live Jita sell prices instead of adjusted (more accurate, more API calls)
"""
import asyncio
import argparse
import os
import sys
import sqlite3
import httpx
from rich.console import Console
from rich.table import Table
from rich import box

from app.auth.token_store import get_valid_token, get_character, is_logged_in
from app.esi.client import search_type_by_name
from app.cache.blueprint_cache import resolve_type
from app.db.database import get_session
from app.db.type_resolver import resolve_names_bulk
from app.character.blueprints import fetch_blueprints, ensure_bp_table
from app.character.assets import fetch_assets, ensure_assets_table, assets_at_location
from app.manufacturing.planner import build_plan, find_blueprint_for_product
from app.manufacturing.display import print_plan
from app.market.prices import fetch_adjusted_prices, fetch_jita_prices_bulk, ensure_price_table

console = Console()
DB_ABS = os.path.abspath(os.path.join(os.path.dirname(__file__), "eve_cache.db"))


def collect_all_type_ids(node) -> list[int]:
    """Recursively collect all type_ids from the BOM tree."""
    ids = [node.type_id]
    for child in node.children:
        ids.extend(collect_all_type_ids(child))
    return ids


_location_cache: dict[int, str] = {}
_LOC_SEM = asyncio.Semaphore(10)
ESI_BASE = "https://esi.evetech.net/latest"


async def resolve_station_name(
    client: httpx.AsyncClient,
    location_id: int,
    token: str | None = None,
) -> str:
    """Resolve a location ID to a name. NPC stations and player structures (with token)."""
    if location_id in _location_cache:
        return _location_cache[location_id]

    name = str(location_id)
    async with _LOC_SEM:
        try:
            if location_id < 1_000_000_000_000:
                # NPC station
                r = await client.get(
                    f"{ESI_BASE}/universe/stations/{location_id}/",
                    params={"datasource": "tranquility"},
                    timeout=10,
                )
                if r.status_code == 200:
                    name = r.json().get("name", name)
            else:
                # Player structure — requires a token
                if token:
                    r = await client.get(
                        f"{ESI_BASE}/universe/structures/{location_id}/",
                        params={"datasource": "tranquility"},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        name = r.json().get("name", name)
                    elif r.status_code == 403:
                        name = f"[Private structure {location_id}]"
        except Exception:
            pass

    _location_cache[location_id] = name
    return name


async def resolve_station_names_bulk(
    location_ids: list[int],
    token: str | None = None,
) -> dict[int, str]:
    """Resolve a list of location_ids to names in parallel."""
    async with httpx.AsyncClient() as client:
        tasks = [resolve_station_name(client, lid, token) for lid in location_ids]
        names = await asyncio.gather(*tasks)
    return dict(zip(location_ids, names))


async def list_blueprints(char_id: int, token: str, conn: sqlite3.Connection):
    """List the character's blueprints. Unknown type_ids are resolved via ESI."""
    async with httpx.AsyncClient() as client:
        console.print("[dim]Loading blueprints...[/]")
        bps = await fetch_blueprints(client, char_id, token, conn)

        if not bps:
            console.print("[yellow]No blueprints found.[/]")
            return

        # Resolve all type_ids at once (SDE + ESI fallback for unknowns)
        unique_ids = list({bp.type_id for bp in bps})
        names = await resolve_names_bulk(conn, unique_ids, client)

        # Optionally report newly filled-in types
        newly_resolved = [
            names[tid] for tid in unique_ids
            if not conn.execute("SELECT 1 FROM sde_types WHERE type_id=? AND name NOT LIKE 'Unknown%'", (tid,)).fetchone()
               and not names[tid].startswith("Unknown")
        ]
        if newly_resolved:
            console.print(f"[dim]Filled in from ESI: {', '.join(newly_resolved)}[/]")

    table = Table(title=f"Character blueprints ({len(bps)})", box=box.ROUNDED, show_lines=True)
    table.add_column("Blueprint name", style="cyan", min_width=36)
    table.add_column("Type",  justify="center")
    table.add_column("ME",    justify="right")
    table.add_column("TE",    justify="right")
    table.add_column("Runs",  justify="right")
    table.add_column("Loc ID", style="dim", justify="right")

    for bp in sorted(bps, key=lambda b: names.get(b.type_id, "")):
        name = names.get(bp.type_id, f"Unknown ({bp.type_id})")
        kind = "[green]BPO[/]" if bp.is_original else "[yellow]BPC[/]"
        runs = "∞" if bp.runs == -1 else str(bp.runs)
        table.add_row(name, kind, str(bp.material_efficiency), str(bp.time_efficiency), runs, str(bp.location_id))

    console.print()
    console.print(table)


async def list_locations(char_id: int, token: str, conn: sqlite3.Connection):
    """List locations where the character has materials (bulk name resolution)."""
    async with httpx.AsyncClient() as client:
        console.print("[dim]Loading assets...[/]")
        assets = await fetch_assets(client, char_id, token, conn)

    # Group by location_id
    locations: dict[int, int] = {}
    for a in assets:
        if not a.is_singleton:
            locations[a.location_id] = locations.get(a.location_id, 0) + 1

    if not locations:
        console.print("[yellow]No materials found.[/]")
        return

    # Resolve all names in parallel
    console.print(f"[dim]Resolving names for {len(locations)} locations...[/]")
    loc_names = await resolve_station_names_bulk(list(locations.keys()), token)

    table = Table(title="Locations with materials", box=box.ROUNDED)
    table.add_column("Location ID",  style="dim",  justify="right")
    table.add_column("Station name", style="cyan", min_width=44)
    table.add_column("Stack count",  justify="right")

    for loc_id, count in sorted(locations.items(), key=lambda x: -x[1]):
        name = loc_names.get(loc_id, str(loc_id))
        table.add_row(str(loc_id), name, str(count))

    console.print()
    console.print(table)


async def main():
    parser = argparse.ArgumentParser(description="EVE Retroindustry — production planner")
    parser.add_argument("--product",         help="Product name or type_id")
    parser.add_argument("--station", type=int, help="Station/structure ID (e.g. 60003760 = Jita)")
    parser.add_argument("--qty",     type=int, default=1, help="Number of units (default: 1)")
    parser.add_argument("--list-blueprints", action="store_true", help="List your blueprints")
    parser.add_argument("--list-locations",  action="store_true", help="List locations with materials")
    parser.add_argument("--refresh",         action="store_true", help="Force reload of data from ESI")
    parser.add_argument("--jita",            action="store_true", help="Live Jita prices instead of adjusted")
    parser.add_argument("--mode",            default="full",
                        choices=["full", "components", "optimal"],
                        help="full=raw materials | components=1st level | optimal=make vs buy")
    args = parser.parse_args()

    # Verify login
    if not is_logged_in():
        console.print("[red]Not logged in. Run: python login.py --client-id <ID>[/]")
        sys.exit(1)

    token = get_valid_token()
    char  = get_character()
    if not token or not char:
        console.print("[red]Token or character data missing. Log in again.[/]")
        sys.exit(1)

    char_id, char_name = char
    console.print(f"\n[bold]Character: [cyan]{char_name}[/] (ID: {char_id})[/]")

    # Initialize DB tables
    conn = sqlite3.connect(DB_ABS)
    ensure_bp_table(conn)
    ensure_assets_table(conn)

    if args.list_blueprints:
        await list_blueprints(char_id, token, conn)
        conn.close()
        return

    if args.list_locations:
        await list_locations(char_id, token, conn)
        conn.close()
        return

    if not args.product:
        console.print("[red]Provide --product or use --list-blueprints / --list-locations[/]")
        parser.print_help()
        conn.close()
        sys.exit(1)

    if not args.station:
        console.print("[red]Provide --station <location_id>. Available stations: python plan.py --list-locations[/]")
        conn.close()
        sys.exit(1)

    # Resolve product to type_id
    if args.product.isdigit():
        type_id = int(args.product)
        async with httpx.AsyncClient() as client:
            session = get_session()
            type_name = await resolve_type(client, session, type_id)
            session.close()
    else:
        async with httpx.AsyncClient() as client:
            session = get_session()
            results = await search_type_by_name(client, args.product)
            if not results:
                console.print(f"[red]Product '{args.product}' not found.[/]")
                conn.close()
                sys.exit(1)
            type_id = results[0]
            type_name = await resolve_type(client, session, type_id)
            session.close()

    console.print(f"  Product: [cyan]{type_name}[/] (ID: {type_id}) ×{args.qty}")

    # Load the character's blueprints and assets
    async with httpx.AsyncClient() as client:
        console.print("[dim]Loading character blueprints...[/]")
        blueprints = await fetch_blueprints(client, char_id, token, conn, force_refresh=args.refresh)
        console.print(f"[dim]Found {len(blueprints)} blueprints.[/]")

        console.print("[dim]Loading assets at station...[/]")
        all_assets = await fetch_assets(client, char_id, token, conn, force_refresh=args.refresh)
        console.print(f"[dim]Total assets: {len(all_assets)}[/]")

    available = assets_at_location(all_assets, args.station)
    console.print(f"[dim]Materials at station {args.station}: {len(available)} kinds[/]")

    async with httpx.AsyncClient() as client:
        station_name = await resolve_station_name(client, args.station, token)

    # Pick ME from the character's blueprint (needed before loading prices)
    _bp = find_blueprint_for_product(blueprints, type_id, conn)
    _me = float(_bp.material_efficiency if _bp else 0)

    # Build the BOM tree — needed both for collecting type_ids and for optimal mode
    from app.bom.resolver import BOMResolver as _BOMResolver
    _resolver = _BOMResolver(DB_ABS)
    _root = _resolver.resolve(type_id, args.qty, me=_me)
    _resolver.close()

    # Collect type_ids — we need every node in the tree plus the product itself
    all_ids = list(set(collect_all_type_ids(_root) + [type_id]))
    async with httpx.AsyncClient() as client:
        if args.jita:
            console.print(f"[dim]Loading live Jita prices for {len(all_ids)} types...[/]")
            ensure_price_table(conn)
            prices = await fetch_jita_prices_bulk(client, conn, all_ids)
        else:
            console.print("[dim]Loading adjusted prices (ESI)...[/]")
            adj = await fetch_adjusted_prices(client)
            prices = {
                tid: (adj[tid].get("average_price") if tid in adj else None, None)
                for tid in all_ids
            }

    # Build the plan
    plan = build_plan(
        product_type_id  = type_id,
        quantity         = args.qty,
        location_id      = args.station,
        available_assets = available,
        blueprints       = blueprints,
        db_path          = DB_ABS,
        mode             = args.mode,
        prices           = prices,
    )

    print_plan(plan, location_name=station_name, prices=prices)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
