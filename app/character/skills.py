"""Loading and caching character skills from ESI."""
from __future__ import annotations
import json
import sqlite3
import time
import httpx

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 3600  # 1 hour

# Industry and AdvIndustry are applied separately in calc_job_time,
# but we must always fetch them for display in the UI.
_GENERAL_SKILL_IDS = {3380, 3388}

# Cache schema version — bump it when the format changes to force a refresh.
# v2: we store all skills from ESI (previously only a filtered subset of
# manufacturing + science skills was stored, so blueprint-required skills like
# Capital Ship Construction were missing and showed up red in the UI even for a
# character who had them).
_CACHE_VERSION = 2


async def fetch_skill_queue(client: httpx.AsyncClient, character_id: int, access_token: str) -> list[dict]:
    """Returns the skill queue from ESI (sorted by queue_position). Empty list =
    no active skill training. Requires scope esi-skills.read_skillqueue.v1."""
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
    """Returns the character's current location from ESI: {solar_system_id,
    station_id?, structure_id?}. Empty dict on error. Scope
    esi-location.read_location.v1."""
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
    """Returns the set of type_ids of all skills relevant to manufacturing (science + Industry/AdvIndustry)."""
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
    """Returns (version, skills_dict). Version 0 = old flat schema (filtered subset)."""
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
    """Returns cached skills if the cache is fresh AND in the current schema version."""
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
        return None  # old schema → force a refresh
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
    """Returns {skill_type_id: trained_level} for all of the character's trained skills."""
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
            # Fallback — if we have anything cached (even stale), use it.
            return get_cached_skills(conn, character_id)
        all_skills = {int(s["skill_id"]): int(s["trained_skill_level"])
                      for s in r.json().get("skills", [])}
        _save_cache(conn, character_id, all_skills)
        return all_skills
    except Exception:
        return get_cached_skills(conn, character_id)


def get_cached_skills(conn: sqlite3.Connection, character_id: int) -> dict[int, int]:
    """Loads skills from the DB without an ESI call. Returns an empty dict if the cache doesn't exist."""
    row = conn.execute(
        "SELECT data_json FROM char_skills_cache WHERE character_id=?", (character_id,)
    ).fetchone()
    if not row:
        return {}
    _, skills = _parse_blob(row[0])
    return skills
