"""
Resolving type_id → name.
Priority: local SDE → ESI (storing into sde_types for next time).
"""
import asyncio
import sqlite3
import httpx

ESI_BASE = "https://esi.evetech.net/latest"
_ESI_SEM = asyncio.Semaphore(10)


def resolve_name_sync(conn: sqlite3.Connection, type_id: int) -> str | None:
    """Return the name from the local SDE. None if missing."""
    row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (type_id,)).fetchone()
    return row[0] if row else None


def _save_to_sde(conn: sqlite3.Connection, type_id: int, name: str,
                 group_id: int | None, published: bool):
    conn.execute(
        "INSERT OR REPLACE INTO sde_types (type_id, name, group_id, published) VALUES (?,?,?,?)",
        (type_id, name, group_id, 1 if published else 0)
    )
    conn.commit()


async def _fetch_from_esi(client: httpx.AsyncClient, type_id: int) -> dict | None:
    async with _ESI_SEM:
        r = await client.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            params={"datasource": "tranquility", "language": "en"},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None


async def resolve_name(
    conn: sqlite3.Connection,
    type_id: int,
    client: httpx.AsyncClient,
) -> str:
    """
    Return the type name. If missing from the SDE, query ESI and store the result.
    """
    name = resolve_name_sync(conn, type_id)
    if name:
        return name

    data = await _fetch_from_esi(client, type_id)
    if data:
        name = data.get("name", f"Unknown ({type_id})")
        _save_to_sde(conn, type_id, name, data.get("group_id"), data.get("published", True))
        return name

    return f"Unknown ({type_id})"


async def resolve_names_bulk(
    conn: sqlite3.Connection,
    type_ids: list[int],
    client: httpx.AsyncClient,
) -> dict[int, str]:
    """Translate a list of type_ids to names in parallel (SDE + ESI fallback)."""
    tasks = [resolve_name(conn, tid, client) for tid in type_ids]
    names = await asyncio.gather(*tasks)
    return dict(zip(type_ids, names))
