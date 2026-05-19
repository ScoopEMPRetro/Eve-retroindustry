import json
import time
import httpx
from sqlalchemy.orm import Session
from app.db.database import TypeCache, BlueprintCache, get_session
from app.esi.client import fetch_type_info, fetch_blueprint_data

CACHE_TTL = 60 * 60 * 24 * 7  # 7 dní — blueprinty se nemění často


def get_type_name(session: Session, type_id: int) -> str | None:
    row = session.get(TypeCache, type_id)
    return row.name if row else None


def save_type(session: Session, type_id: int, name: str, group_id: int = None, category_id: int = None):
    existing = session.get(TypeCache, type_id)
    if existing:
        return
    session.add(TypeCache(type_id=type_id, name=name, group_id=group_id, category_id=category_id))
    session.commit()


def get_cached_blueprint(session: Session, type_id: int) -> dict | None:
    row = session.get(BlueprintCache, type_id)
    if not row:
        return None
    if time.time() - (row.cached_at or 0) > CACHE_TTL:
        return None  # expired
    return json.loads(row.data_json)


def save_blueprint(session: Session, type_id: int, blueprint_type_id: int, data: dict):
    existing = session.get(BlueprintCache, type_id)
    if existing:
        existing.data_json = json.dumps(data)
        existing.cached_at = time.time()
        existing.blueprint_type_id = blueprint_type_id
    else:
        session.add(BlueprintCache(
            type_id=type_id,
            blueprint_type_id=blueprint_type_id,
            data_json=json.dumps(data),
            cached_at=time.time(),
        ))
    session.commit()


async def resolve_type(client: httpx.AsyncClient, session: Session, type_id: int) -> str:
    """Vrátí název typu — z cache nebo z ESI."""
    cached = get_type_name(session, type_id)
    if cached:
        return cached

    data = await fetch_type_info(client, type_id)
    if not data:
        return f"Unknown ({type_id})"

    name = data.get("name", f"Unknown ({type_id})")
    save_type(session, type_id, name, data.get("group_id"), data.get("category_id"))
    return name


async def resolve_blueprint(client: httpx.AsyncClient, session: Session, type_id: int) -> dict | None:
    """
    Vrátí blueprint data pro daný type_id produktu.
    Fuzzwork vrací dict: { "blueprint_type_id": { "activities": {...} } }
    """
    cached = get_cached_blueprint(session, type_id)
    if cached:
        return cached

    data = await fetch_blueprint_data(client, type_id)
    if not data:
        return None

    # Uložíme do cache
    bp_type_id = int(list(data.keys())[0])
    save_blueprint(session, type_id, bp_type_id, data)
    return data
