"""
EVE Retroindustry — login via ESI OAuth2.

Usage (first login):
  python login.py --client-id <YOUR_CLIENT_ID>

Repeat login (client_id already stored):
  python login.py

How to get a client_id:
  1. Go to https://developers.eveonline.com/
  2. Create New Application
  3. Connection Type: Authentication & API Access
  4. Scopes: esi-characters.read_blueprints.v1  esi-assets.read_assets.v1
  5. Callback URL: http://localhost:5173/callback
  6. Copy the Client ID (no secret key — native app)
"""
import argparse
import asyncio
from rich.console import Console
from app.auth.esi_oauth import login
from app.auth.token_store import get_character, is_logged_in

console = Console()


def main():
    parser = argparse.ArgumentParser(description="EVE Retroindustry — ESI Login")
    parser.add_argument("--client-id", help="EVE Application Client ID")
    parser.add_argument("--status",    action="store_true", help="Show login status")
    args = parser.parse_args()

    if args.status:
        if is_logged_in():
            char = get_character()
            if char:
                console.print(f"[green]Logged in as: {char[1]} (ID: {char[0]})[/]")
            else:
                console.print("[green]Token valid, but character info is missing.[/]")
        else:
            console.print("[red]Not logged in.[/]")
        return

    success = login(client_id=args.client_id)
    if success:
        console.print("\n[bold green]Login successful. You can now use:[/]")
        console.print("  python plan.py --product 'Nidhoggur' --station 60003760")
    else:
        console.print("\n[red]Login failed.[/]")


if __name__ == "__main__":
    main()
