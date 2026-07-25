import httpx
import asyncio
from typing import Optional

ESI_BASE = "https://esi.evetech.net/latest"
FUZZWORK_BASE = "https://www.fuzzwork.co.uk"

# ESI date-based versioning: pin behavior to a fixed date (X-Compatibility-Date)
# so future breaking changes don't break us. Change the date only on a deliberate switch
# to newer API behavior. /latest in the URL still works; the header takes precedence.
ESI_COMPAT_DATE = "2026-07-17"


# The connection pool must cover our concurrency (semaphores up to 30), otherwise refresh
# is the bottleneck: httpx's default max_keepalive_connections=20 recycles only ~20
# connections and the rest pay the TLS handshake over and over — with keepalive 50 the bulk
# volume/orders refresh is ~2.8x faster (measured). We stay at 30 concurrent
# (semaphore), so under the ESI rate limit.
_ESI_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=50)


def esi_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient with a preset X-Compatibility-Date header and a
    connection pool sized for our concurrency (see _ESI_LIMITS). For
    non-ESI hosts (GitHub, images) both are harmless. Per-request headers are
    merged with the client header; the caller can override limits via kwargs."""
    headers = {"X-Compatibility-Date": ESI_COMPAT_DATE}
    headers.update(kwargs.pop("headers", None) or {})
    kwargs.setdefault("limits", _ESI_LIMITS)
    return httpx.AsyncClient(headers=headers, **kwargs)

# Rate limiting: ESI allows ~150 req/s, Fuzzwork is slower
ESI_SEMAPHORE = asyncio.Semaphore(20)
FUZZ_SEMAPHORE = asyncio.Semaphore(5)


async def fetch_type_info(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """Fetches the type's name and category from ESI."""
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
    Fetches blueprint data from the Fuzzwork API.
    type_id is the *product* ID (not the blueprint's).
    Returns manufacturing/reaction activities with a list of materials.
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
        # Fuzzwork returns a dict where the key is the blueprint_type_id
        return data if data else None


async def search_type_by_name(client: httpx.AsyncClient, name: str) -> list[int]:
    """Converts a name to a type_id via ESI /universe/ids/ (POST)."""
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
    """Fetches information about multiple types at once."""
    tasks = [fetch_type_info(client, tid) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res
        for tid, res in zip(type_ids, results)
        if isinstance(res, dict)
    }
