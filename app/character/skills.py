"""Načítání a cache skillů postavy z ESI."""
from __future__ import annotations
import json
import sqlite3
import time
import httpx

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 3600  # 1 hodina

# Industry a AdvIndustry jsou aplikovány zvlášť v calc_job_time,
# ale musíme je vždy fetchovat pro zobrazení v UI.
_GENERAL_SKILL_IDS = {3380, 3388}

# Cache schema version — bumpni při změně formátu pro vynucení refreshe.
# v2: ukládáme všechny skilly z ESI (předtím se ukládal jen filtrovaný subset
# výrobních + science skillů, takže blueprint-required skilly jako Capital Ship
# Construction chyběly a v UI se ukazovaly červeně i pro postavu, která je má).
_CACHE_VERSION = 2


async def fetch_skill_queue(client: httpx.AsyncClient, character_id: int, access_token: str) -> list[dict]:
    """Vrátí frontu skillů z ESI (seřazeno dle queue_position). Prázdný list =
    žádné aktivní skillování. Vyžaduje scope esi-skills.read_skillqueue.v1."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/skillqueue/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            q = r.json()
            return sorted(q, key=lambda e: e.get("queue_position", 0)) if isinstance(q, list) else []
    except Exception:
        pass
    return []


async def fetch_location(client: httpx.AsyncClient, character_id: int, access_token: str) -> dict:
    """Vrátí aktuální polohu postavy z ESI: {solar_system_id, station_id?,
    structure_id?}. Prázdný dict při chybě. Scope esi-location.read_location.v1."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/location/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200 and isinstance(r.json(), dict):
            return r.json()
    except Exception:
        pass
    return {}


def get_mfg_skill_ids(conn: sqlite3.Connection) -> set[int]:
    """Vrátí set type_id všech skillů relevantních pro výrobu (science + Industry/AdvIndustry)."""
    try:
        rows = conn.execute("SELECT skill_type_id FROM sde_skill_time_bonus").fetchall()
        science_ids = {r[0] for r in rows}
    except sqlite3.OperationalError:
        science_ids = set()
    return science_ids | _GENERAL_SKILL_IDS


def ensure_skills_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS char_skills_cache (
            character_id INTEGER PRIMARY KEY,
            data_json    TEXT NOT NULL,
            cached_at    REAL NOT NULL
        )
    """)
    conn.commit()


def _parse_blob(raw: str) -> tuple[int, dict[int, int]]:
    """Vrátí (version, skills_dict). Verze 0 = staré ploché schéma (filtrovaný subset)."""
    try:
        data = json.loads(raw)
    except Exception:
        return 0, {}
    if isinstance(data, dict) and "__v" in data:
        skills = data.get("skills") or {}
        return int(data.get("__v", 0)), {int(k): int(v) for k, v in skills.items()}
    if isinstance(data, dict):
        return 0, {int(k): int(v) for k, v in data.items()}
    return 0, {}


def _load_cache_fresh(conn: sqlite3.Connection, character_id: int) -> dict[int, int] | None:
    """Vrátí cached skilly pokud je cache čerstvá A v aktuální verzi schématu."""
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_skills_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if not row:
        return None
    if (time.time() - row[1]) >= CACHE_TTL:
        return None
    version, skills = _parse_blob(row[0])
    if version != _CACHE_VERSION:
        return None  # staré schéma → vynutíme refresh
    return skills


def _save_cache(conn: sqlite3.Connection, character_id: int, skills: dict[int, int]):
    blob = json.dumps({
        "__v": _CACHE_VERSION,
        "skills": {str(k): int(v) for k, v in skills.items()},
    })
    conn.execute(
        "INSERT OR REPLACE INTO char_skills_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, blob, time.time())
    )
    conn.commit()


async def fetch_skills(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> dict[int, int]:
    """Vrátí {skill_type_id: trained_level} pro všechny trénované skilly postavy."""
    if not force_refresh:
        cached = _load_cache_fresh(conn, character_id)
        if cached is not None:
            return cached

    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/skills/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.status_code != 200:
            # Fallback — pokud máme něco v cache (i staré), použij to.
            return get_cached_skills(conn, character_id)
        all_skills = {int(s["skill_id"]): int(s["trained_skill_level"])
                      for s in r.json().get("skills", [])}
        _save_cache(conn, character_id, all_skills)
        return all_skills
    except Exception:
        return get_cached_skills(conn, character_id)


def get_cached_skills(conn: sqlite3.Connection, character_id: int) -> dict[int, int]:
    """Načte skilly z DB bez ESI volání. Vrátí prázdný dict pokud cache neexistuje."""
    row = conn.execute(
        "SELECT data_json FROM char_skills_cache WHERE character_id=?", (character_id,)
    ).fetchone()
    if not row:
        return {}
    _, skills = _parse_blob(row[0])
    return skills
