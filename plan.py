"""
EVE Retroindustry — výrobní plánovač s daty postavy.

Použití:
  python plan.py --product "Phoenix" --station 60003760
  python plan.py --product "Phoenix" --station 60003760 --jita
  python plan.py --product 19726 --station 60003760 --qty 2
  python plan.py --list-blueprints
  python plan.py --list-locations
  python plan.py --refresh

Ceny adjusted prices (1 API call) se zobrazují vždy.
--jita         Živé Jita sell ceny místo adjusted (přesnější, více API callů)
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
    """Rekurzivně sbírá všechna type_id ze stromu BOM."""
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
    """Přeloží location ID na název. NPC stanice i player struktury (s tokenem)."""
    if location_id in _location_cache:
        return _location_cache[location_id]

    name = str(location_id)
    async with _LOC_SEM:
        try:
            if location_id < 1_000_000_000_000:
                # NPC stanice
                r = await client.get(
                    f"{ESI_BASE}/universe/stations/{location_id}/",
                    params={"datasource": "tranquility"},
                    timeout=10,
                )
                if r.status_code == 200:
                    name = r.json().get("name", name)
            else:
                # Player struktura — potřebuje token
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
                        name = f"[Privátní struktura {location_id}]"
        except Exception:
            pass

    _location_cache[location_id] = name
    return name


async def resolve_station_names_bulk(
    location_ids: list[int],
    token: str | None = None,
) -> dict[int, str]:
    """Přeloží seznam location_id na jména paralelně."""
    async with httpx.AsyncClient() as client:
        tasks = [resolve_station_name(client, lid, token) for lid in location_ids]
        names = await asyncio.gather(*tasks)
    return dict(zip(location_ids, names))


async def list_blueprints(char_id: int, token: str, conn: sqlite3.Connection):
    """Vypíše blueprinty postavy. Neznámé type_id resolvuje přes ESI."""
    async with httpx.AsyncClient() as client:
        console.print("[dim]Načítám blueprinty...[/]")
        bps = await fetch_blueprints(client, char_id, token, conn)

        if not bps:
            console.print("[yellow]Žádné blueprinty nalezeny.[/]")
            return

        # Přelož všechna type_id najednou (SDE + ESI fallback pro neznámé)
        unique_ids = list({bp.type_id for bp in bps})
        names = await resolve_names_bulk(conn, unique_ids, client)

        # Případně informuj o nově doplněných typech
        newly_resolved = [
            names[tid] for tid in unique_ids
            if not conn.execute("SELECT 1 FROM sde_types WHERE type_id=? AND name NOT LIKE 'Unknown%'", (tid,)).fetchone()
               and not names[tid].startswith("Unknown")
        ]
        if newly_resolved:
            console.print(f"[dim]Doplněno z ESI: {', '.join(newly_resolved)}[/]")

    table = Table(title=f"Blueprinty postavy ({len(bps)})", box=box.ROUNDED, show_lines=True)
    table.add_column("Název blueprintu", style="cyan", min_width=36)
    table.add_column("Typ",   justify="center")
    table.add_column("ME",    justify="right")
    table.add_column("TE",    justify="right")
    table.add_column("Runy",  justify="right")
    table.add_column("Loc ID", style="dim", justify="right")

    for bp in sorted(bps, key=lambda b: names.get(b.type_id, "")):
        name = names.get(bp.type_id, f"Unknown ({bp.type_id})")
        kind = "[green]BPO[/]" if bp.is_original else "[yellow]BPC[/]"
        runs = "∞" if bp.runs == -1 else str(bp.runs)
        table.add_row(name, kind, str(bp.material_efficiency), str(bp.time_efficiency), runs, str(bp.location_id))

    console.print()
    console.print(table)


async def list_locations(char_id: int, token: str, conn: sqlite3.Connection):
    """Vypíše lokace kde má postava materiály (bulk resolvování jmen)."""
    async with httpx.AsyncClient() as client:
        console.print("[dim]Načítám assety...[/]")
        assets = await fetch_assets(client, char_id, token, conn)

    # Seskup podle location_id
    locations: dict[int, int] = {}
    for a in assets:
        if not a.is_singleton:
            locations[a.location_id] = locations.get(a.location_id, 0) + 1

    if not locations:
        console.print("[yellow]Žádné materiály nalezeny.[/]")
        return

    # Resolvuj všechna jména paralelně
    console.print(f"[dim]Resolvuji jména {len(locations)} lokací...[/]")
    loc_names = await resolve_station_names_bulk(list(locations.keys()), token)

    table = Table(title="Lokace s materiály", box=box.ROUNDED)
    table.add_column("Location ID",   style="dim",  justify="right")
    table.add_column("Název stanice", style="cyan", min_width=44)
    table.add_column("Počet stacků",  justify="right")

    for loc_id, count in sorted(locations.items(), key=lambda x: -x[1]):
        name = loc_names.get(loc_id, str(loc_id))
        table.add_row(str(loc_id), name, str(count))

    console.print()
    console.print(table)


async def main():
    parser = argparse.ArgumentParser(description="EVE Retroindustry — výrobní plánovač")
    parser.add_argument("--product",         help="Název produktu nebo type_id")
    parser.add_argument("--station", type=int, help="Stanice/struktura ID (např. 60003760 = Jita)")
    parser.add_argument("--qty",     type=int, default=1, help="Počet kusů (default: 1)")
    parser.add_argument("--list-blueprints", action="store_true", help="Vypíše tvoje blueprinty")
    parser.add_argument("--list-locations",  action="store_true", help="Vypíše lokace s materiály")
    parser.add_argument("--refresh",         action="store_true", help="Vynutí reload dat z ESI")
    parser.add_argument("--jita",            action="store_true", help="Živé Jita ceny místo adjusted")
    parser.add_argument("--mode",            default="full",
                        choices=["full", "components", "optimal"],
                        help="full=základní suroviny | components=1. úroveň | optimal=make vs buy")
    args = parser.parse_args()

    # Ověření přihlášení
    if not is_logged_in():
        console.print("[red]Nejsi přihlášen. Spusť: python login.py --client-id <ID>[/]")
        sys.exit(1)

    token = get_valid_token()
    char  = get_character()
    if not token or not char:
        console.print("[red]Token nebo character data chybí. Přihlas se znovu.[/]")
        sys.exit(1)

    char_id, char_name = char
    console.print(f"\n[bold]Postava: [cyan]{char_name}[/] (ID: {char_id})[/]")

    # Inicializuj DB tabulky
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
        console.print("[red]Zadej --product nebo použij --list-blueprints / --list-locations[/]")
        parser.print_help()
        conn.close()
        sys.exit(1)

    if not args.station:
        console.print("[red]Zadej --station <location_id>. Dostupné stanice: python plan.py --list-locations[/]")
        conn.close()
        sys.exit(1)

    # Přelož produkt na type_id
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
                console.print(f"[red]Produkt '{args.product}' nenalezen.[/]")
                conn.close()
                sys.exit(1)
            type_id = results[0]
            type_name = await resolve_type(client, session, type_id)
            session.close()

    console.print(f"  Produkt: [cyan]{type_name}[/] (ID: {type_id}) ×{args.qty}")

    # Načti blueprinty a assety postavy
    async with httpx.AsyncClient() as client:
        console.print("[dim]Načítám blueprinty postavy...[/]")
        blueprints = await fetch_blueprints(client, char_id, token, conn, force_refresh=args.refresh)
        console.print(f"[dim]Nalezeno {len(blueprints)} blueprintů.[/]")

        console.print("[dim]Načítám assety na stanici...[/]")
        all_assets = await fetch_assets(client, char_id, token, conn, force_refresh=args.refresh)
        console.print(f"[dim]Celkem assetů: {len(all_assets)}[/]")

    available = assets_at_location(all_assets, args.station)
    console.print(f"[dim]Materiálů na stanici {args.station}: {len(available)} druhů[/]")

    async with httpx.AsyncClient() as client:
        station_name = await resolve_station_name(client, args.station, token)

    # Vyber ME z blueprintu postavy (potřebujeme před načtením cen)
    _bp = find_blueprint_for_product(blueprints, type_id, conn)
    _me = float(_bp.material_efficiency if _bp else 0)

    # Sestav BOM strom — potřebujeme ho pro sběr type_ids i pro optimal mód
    from app.bom.resolver import BOMResolver as _BOMResolver
    _resolver = _BOMResolver(DB_ABS)
    _root = _resolver.resolve(type_id, args.qty, me=_me)
    _resolver.close()

    # Sbíráme type_ids — potřebujeme všechny uzly stromu + samotný produkt
    all_ids = list(set(collect_all_type_ids(_root) + [type_id]))
    async with httpx.AsyncClient() as client:
        if args.jita:
            console.print(f"[dim]Načítám živé Jita ceny pro {len(all_ids)} typů...[/]")
            ensure_price_table(conn)
            prices = await fetch_jita_prices_bulk(client, conn, all_ids)
        else:
            console.print("[dim]Načítám adjusted prices (ESI)...[/]")
            adj = await fetch_adjusted_prices(client)
            prices = {
                tid: (adj[tid].get("average_price") if tid in adj else None, None)
                for tid in all_ids
            }

    # Sestav plán
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
