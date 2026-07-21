import httpx
import asyncio
from typing import Optional

ESI_BASE = "https://esi.evetech.net/latest"
FUZZWORK_BASE = "https://www.fuzzwork.co.uk"

# ESI date-based verzování: pinneme chování k pevnému datu (X-Compatibility-Date),
# ať nás budoucí breaking changes nerozbijou. Datum měň jen při vědomém přechodu
# na novější chování API. /latest v URL zůstává funkční, header má přednost.
ESI_COMPAT_DATE = "2026-07-17"


# Connection pool musí pokrýt naši souběžnost (semafory až 30), jinak je refresh
# úzkým hrdlem: httpx default max_keepalive_connections=20 recykluje jen ~20
# spojení a zbytek platí TLS handshake pořád dokola — s keepalive 50 je bulk
# volume/orders refresh ~2.8× rychlejší (naměřeno). Zůstáváme na 30 souběžných
# (semafor), takže pod ESI rate-limitem.
_ESI_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=50)


def esi_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient s přednastavenou hlavičkou X-Compatibility-Date a
    connection poolem dimenzovaným na náš concurrency (viz _ESI_LIMITS). Pro
    ne-ESI hosty (GitHub, obrázky) je obojí neškodné. Per-request headers se
    s klientskou hlavičkou slučují; volající může limits přebít přes kwargs."""
    headers = {"X-Compatibility-Date": ESI_COMPAT_DATE}
    headers.update(kwargs.pop("headers", None) or {})
    kwargs.setdefault("limits", _ESI_LIMITS)
    return httpx.AsyncClient(headers=headers, **kwargs)

# Rate limiting: ESI dovoluje ~150 req/s, Fuzzwork je pomalejší
ESI_SEMAPHORE = asyncio.Semaphore(20)
FUZZ_SEMAPHORE = asyncio.Semaphore(5)


async def fetch_type_info(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """Načte název a kategorii typu z ESI."""
    async with ESI_SEMAPHORE:
        r = await client.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            params={"datasource": "tranquility", "language": "en"},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def fetch_blueprint_data(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """
    Načte blueprint data z Fuzzwork API.
    type_id je ID *produktu* (ne blueprintu).
    Vrací manufacturing/reaction aktivity se seznamem materiálů.
    """
    async with FUZZ_SEMAPHORE:
        r = await client.get(
            f"{FUZZWORK_BASE}/blueprint/",
            params={"typeID": type_id, "format": "json"},
            timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        # Fuzzwork vrací dict kde klíč je blueprint_type_id
        return data if data else None


async def search_type_by_name(client: httpx.AsyncClient, name: str) -> list[int]:
    """Převede jméno na type_id přes ESI /universe/ids/ (POST)."""
    async with ESI_SEMAPHORE:
        r = await client.post(
            f"{ESI_BASE}/universe/ids/",
            params={"datasource": "tranquility", "language": "en"},
            json=[name],
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        types = data.get("inventory_types", [])
        return [t["id"] for t in types]


async def fetch_types_bulk(client: httpx.AsyncClient, type_ids: list[int]) -> dict[int, dict]:
    """Načte informace o více typech najednou."""
    tasks = [fetch_type_info(client, tid) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res
        for tid, res in zip(type_ids, results)
        if isinstance(res, dict)
    }
