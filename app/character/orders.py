"""
Market orders — active and historical, for character and corporation.

ESI endpoints:
  Character:
    GET /characters/{id}/orders/           → active orders
    GET /characters/{id}/orders/history/   → last ~90 days (paginated)
  Corporation (requires role Accountant/Trader → otherwise 403):
    GET /corporations/{id}/orders/
    GET /corporations/{id}/orders/history/

Scope: esi-markets.read_character_orders.v1 / esi-markets.read_corporation_orders.v1
"""
from __future__ import annotations
import httpx

ESI_BASE = "https://esi.evetech.net/latest"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _get_all(client: httpx.AsyncClient, url: str, token: str, pages: int = 5) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(url, params={"page": page}, headers=_auth(token), timeout=15)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)):
            break
    return out


async def fetch_orders(client, char_id: int, token: str) -> list[dict]:
    """Active orders of a character (single page)."""
    try:
        r = await client.get(f"{ESI_BASE}/characters/{char_id}/orders/",
                             headers=_auth(token), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


async def fetch_orders_history(client, char_id: int, token: str) -> list[dict]:
    return await _get_all(client, f"{ESI_BASE}/characters/{char_id}/orders/history/", token)


async def fetch_corp_orders(client, corp_id: int, token: str
                            ) -> tuple[list[dict] | None, str | None]:
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                             params={"page": 1}, headers=_auth(token), timeout=15)
        if r.status_code == 200:
            out = r.json()
            for page in range(2, int(r.headers.get("x-pages", 1)) + 1):
                rp = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                                     params={"page": page}, headers=_auth(token), timeout=15)
                if rp.status_code != 200:
                    break
                out.extend(rp.json())
            return out, None
        if r.status_code == 403:
            return None, "This character lacks the corporation role to read market orders (Accountant / Trader)."
        return None, f"ESI returned HTTP {r.status_code}."
    except Exception as exc:
        return None, str(exc)


async def fetch_corp_orders_history(client, corp_id: int, token: str) -> list[dict]:
    return await _get_all(client, f"{ESI_BASE}/corporations/{corp_id}/orders/history/", token)
