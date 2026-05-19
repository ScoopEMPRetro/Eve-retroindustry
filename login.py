"""
EVE Retroindustry — přihlášení přes ESI OAuth2.

Použití (první přihlášení):
  python login.py --client-id <YOUR_CLIENT_ID>

Opakované přihlášení (client_id už je uloženo):
  python login.py

Jak získat client_id:
  1. Jdi na https://developers.eveonline.com/
  2. Create New Application
  3. Connection Type: Authentication & API Access
  4. Scopes: esi-characters.read_blueprints.v1  esi-assets.read_assets.v1
  5. Callback URL: http://localhost:5173/callback
  6. Zkopíruj Client ID (bez tajného klíče — native app)
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
    parser.add_argument("--status",    action="store_true", help="Zobraz stav přihlášení")
    args = parser.parse_args()

    if args.status:
        if is_logged_in():
            char = get_character()
            if char:
                console.print(f"[green]Přihlášen jako: {char[1]} (ID: {char[0]})[/]")
            else:
                console.print("[green]Token platný, ale character info chybí.[/]")
        else:
            console.print("[red]Nepřihlášen.[/]")
        return

    success = login(client_id=args.client_id)
    if success:
        console.print("\n[bold green]Přihlášení úspěšné. Nyní můžeš použít:[/]")
        console.print("  python plan.py --product 'Nidhoggur' --station 60003760")
    else:
        console.print("\n[red]Přihlášení selhalo.[/]")


if __name__ == "__main__":
    main()
