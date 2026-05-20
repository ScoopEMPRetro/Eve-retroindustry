"""Načítání a cache skillů postavy z ESI."""
from __future__ import annotations
import json
import sqlite3
import time
import httpx

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 3600  # 1 hodina

# Fallback pokud sde_skill_time_bonus tabulka ještě neexistuje
_FALLBACK_SKILL_IDS = {3380, 3388}


def get_mfg_skill_ids(conn: sqlite3.Connection) -> set[int]:
    """Vrátí set type_id všech skillů, které mají time bonus v SDE."""
    try:
        rows = conn.execute("SELECT skill_type_id FROM sde_skill_time_bonus").fetchall()
        return {r[0] for r in rows} if rows else _FALLBACK_SKILL_IDS
    except sqlite3.OperationalError:
        return _FALLBACK_SKILL_IDS


def ensure_skills_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS char_skills_cache (
            character_id INTEGER PRIMARY KEY,
            data_json    TEXT NOT NULL,
            cached_at    REAL NOT NULL
        )
    """)
    conn.commit()


def _load_cache(conn: sqlite3.Connection, character_id: int) -> dict[int, int] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_skills_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return {int(k): v for k, v in json.loads(row[0]).items()}
    return None


def _save_cache(conn: sqlite3.Connection, character_id: int, skills: dict[int, int]):
    conn.execute(
        "INSERT OR REPLACE INTO char_skills_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, json.dumps({str(k): v for k, v in skills.items()}), time.time())
    )
    conn.commit()


async def fetch_skills(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> dict[int, int]:
    """Vrátí {type_id: trained_level} pro všechny výrobní skilly s time bonusem."""
    skill_ids = get_mfg_skill_ids(conn)

    if not force_refresh:
        cached = _load_cache(conn, character_id)
        if cached is not None:
            # Doplň nové skill IDs (SDE mohlo přibýt skillů od posledního fetche)
            if skill_ids.issubset(cached.keys()):
                return cached

    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/skills/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return {sid: 0 for sid in skill_ids}
        all_skills = {s["skill_id"]: s["trained_skill_level"] for s in r.json().get("skills", [])}
        result = {sid: all_skills.get(sid, 0) for sid in skill_ids}
        _save_cache(conn, character_id, result)
        return result
    except Exception:
        return {sid: 0 for sid in skill_ids}


def get_cached_skills(conn: sqlite3.Connection, character_id: int) -> dict[int, int]:
    """Načte skilly z DB bez ESI volání. Vrátí nuly pokud cache neexistuje."""
    skill_ids = get_mfg_skill_ids(conn)
    row = conn.execute(
        "SELECT data_json FROM char_skills_cache WHERE character_id=?", (character_id,)
    ).fetchone()
    if not row:
        return {sid: 0 for sid in skill_ids}
    cached = {int(k): v for k, v in json.loads(row[0]).items()}
    # Doplň případné chybějící skill IDs
    return {sid: cached.get(sid, 0) for sid in skill_ids}
