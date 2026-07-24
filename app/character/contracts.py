"""
Kontrakty — osobní, korporační a veřejné (regionální).

ESI endpointy:
  Postava (scope esi-contracts.read_character_contracts.v1):
    GET /characters/{id}/contracts/                     → kontrakty (paginated)
    GET /characters/{id}/contracts/{cid}/items/         → položky
  Korporace (scope esi-contracts.read_corporation_contracts.v1, role Accountant):
    GET /corporations/{id}/contracts/
    GET /corporations/{id}/contracts/{cid}/items/
  Veřejné (bez auth):
    GET /contracts/public/{region_id}/                  → metadata (paginated)
    GET /contracts/public/items/{cid}/                  → položky
"""
from __future__ import annotations
import asyncio
import httpx

ESI_BASE = "https://esi.evetech.net/latest"

CONTRACT_TYPE_LABELS: dict[str, str] = {
    "item_exchange": "Item Exchange",
    "auction": "Auction",
    "courier": "Courier",
    "loan": "Loan",
    "unknown": "Unknown",
}

CONTRACT_STATUS_LABELS: dict[str, str] = {
    "outstanding": "Outstanding",
    "in_progress": "In Progress",
    "finished_issuer": "Finished (issuer)",
    "finished_contractor": "Finished (contractor)",
    "finished": "Finished",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "failed": "Failed",
    "deleted": "Deleted",
    "reversed": "Reversed",
}


def type_label(t: str) -> str:
    return CONTRACT_TYPE_LABELS.get(t, t or "Unknown")


def status_label(s: str) -> str:
    return CONTRACT_STATUS_LABELS.get(s, s or "")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── Osobní / korporační ──────────────────────────────────────────────────────

async def _get_all_pages(client: httpx.AsyncClient, url: str, token: str | None = None,
                         max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    headers = _auth(token) if token else {"Accept": "application/json"}
    for page in range(1, max_pages + 1):
        try:
            r = await client.get(url, params={"page": page}, headers=headers, timeout=20)
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


async def fetch_character_contracts(client, char_id: int, token: str) -> list[dict]:
    return await _get_all_pages(client, f"{ESI_BASE}/characters/{char_id}/contracts/", token)


async def fetch_corp_contracts(client, corp_id: int, token: str
                               ) -> tuple[list[dict] | None, str | None]:
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/contracts/",
                             params={"page": 1}, headers=_auth(token), timeout=20)
    except Exception as exc:
        return None, str(exc)
    if r.status_code == 403:
        return None, "Tato postava nemá v korporaci roli pro čtení kontraktů (Accountant)."
    if r.status_code != 200:
        return None, f"ESI vrátilo HTTP {r.status_code}."
    out = r.json()
    for page in range(2, int(r.headers.get("x-pages", 1)) + 1):
        try:
            rp = await client.get(f"{ESI_BASE}/corporations/{corp_id}/contracts/",
                                 params={"page": page}, headers=_auth(token), timeout=20)
        except Exception:
            break
        if rp.status_code != 200:
            break
        out.extend(rp.json())
    return out, None


async def fetch_character_contract_items(client, char_id: int, contract_id: int,
                                         token: str) -> list[dict]:
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/contracts/{contract_id}/items/",
            headers=_auth(token), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


async def fetch_corp_contract_items(client, corp_id: int, contract_id: int,
                                    token: str) -> list[dict]:
    try:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/contracts/{contract_id}/items/",
            headers=_auth(token), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


# ── Veřejné (regionální) ─────────────────────────────────────────────────────

_PUB_SEM = asyncio.Semaphore(30)   # pod ESI rate-limit útesem (~45)


async def _fetch_public_page(client, region_id: int, page: int) -> tuple[list[dict], int]:
    async with _PUB_SEM:
        for attempt in range(3):
            try:
                r = await client.get(f"{ESI_BASE}/contracts/public/{region_id}/",
                                     params={"page": page}, timeout=25)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return [], 0
            if r.status_code in (420, 429):
                await asyncio.sleep(min(int(r.headers.get("Retry-After", 30)), 60))
                continue
            if r.status_code != 200:
                return [], 0
            return r.json(), int(r.headers.get("x-pages", 1))
        return [], 0


async def fetch_public_contracts(client, region_id: int, progress_cb=None) -> list[dict]:
    """Všechny veřejné kontrakty v regionu (jen metadata, bez položek)."""
    first, total_pages = await _fetch_public_page(client, region_id, 1)
    if not first and total_pages == 0:
        return []
    pages: list[list[dict]] = [first]
    done = [1]
    lock = asyncio.Lock()

    async def _one(p: int):
        data, _ = await _fetch_public_page(client, region_id, p)
        async with lock:
            pages.append(data)
            done[0] += 1
            if progress_cb:
                res = progress_cb(done[0], total_pages)
                if asyncio.iscoroutine(res):
                    await res

    if progress_cb:
        res = progress_cb(1, total_pages)
        if asyncio.iscoroutine(res):
            await res
    await asyncio.gather(*[_one(p) for p in range(2, total_pages + 1)], return_exceptions=True)
    return [c for page in pages for c in page]


async def fetch_public_contract_items(client, contract_id: int) -> list[dict]:
    """Položky veřejného kontraktu. 204/403/404 → prázdný list (courier bez
    položek, expirovaný apod.)."""
    async with _PUB_SEM:
        try:
            r = await client.get(f"{ESI_BASE}/contracts/public/items/{contract_id}/",
                                 params={"page": 1}, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return []
