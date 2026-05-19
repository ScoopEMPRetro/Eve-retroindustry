"""
Načítání assetů postavy z ESI (stránkovaně).
Vrací materiály dostupné na dané stanici/struktuře.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
import sqlite3
import json
import httpx

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 60 * 10  # 10 minut (assety se mění)


@dataclass
class CharAsset:
    item_id:            int
    type_id:            int
    location_id:        int
    location_flag:      str
    quantity:           int
    is_singleton:       bool   # True = jedinečný předmět (loď, fitted module…)
    is_blueprint_copy:  bool   # True = BPC (kopie blueprintu bez tržní ceny)


def ensure_assets_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS char_assets_cache (
            character_id INTEGER NOT NULL,
            data_json    TEXT NOT NULL,
            cached_at    REAL
        )
    """)
    conn.commit()


def _load_cache(conn: sqlite3.Connection, character_id: int) -> list[dict] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_assets_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_cache(conn: sqlite3.Connection, character_id: int, data: list[dict]):
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (character_id,))
    conn.execute(
        "INSERT INTO char_assets_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, json.dumps(data), time.time())
    )
    conn.commit()


async def fetch_assets(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> list[CharAsset]:
    """Načte všechny assety postavy (stránkovaně), s cache."""
    if not force_refresh:
        cached = _load_cache(conn, character_id)
        if cached is not None:
            return _parse_assets(cached)

    headers = {"Authorization": f"Bearer {access_token}"}
    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/assets/",
            params={"datasource": "tranquility", "page": page},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        items = r.json()
        all_items.extend(items)

        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
        page += 1

    _save_cache(conn, character_id, all_items)
    return _parse_assets(all_items)


def _parse_assets(raw: list[dict]) -> list[CharAsset]:
    result = []
    for item in raw:
        result.append(CharAsset(
            item_id            = item["item_id"],
            type_id            = item["type_id"],
            location_id        = item["location_id"],
            location_flag      = item.get("location_flag", "Hangar"),
            quantity           = item.get("quantity", 1),
            is_singleton       = item.get("is_singleton", False),
            is_blueprint_copy  = item.get("is_blueprint_copy", False),
        ))
    return result


def assets_at_location(assets: list[CharAsset], location_id: int) -> dict[int, int]:
    """
    Vrátí {type_id: total_quantity} pro danou stanici/strukturu.
    Ignoruje singletony (lodě, unikátní předměty).
    """
    result: dict[int, int] = {}
    for a in assets:
        if a.location_id != location_id or a.is_singleton:
            continue
        result[a.type_id] = result.get(a.type_id, 0) + a.quantity
    return result
