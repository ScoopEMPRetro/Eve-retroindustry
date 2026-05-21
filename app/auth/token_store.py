"""
Multi-character token storage.

Tokens (refresh_token, access_token) jsou uloženy v DB tabulce `characters`.
client_id zůstává v .eve_config.json (jediná aplikační hodnota, ne per-char).

Public API:
- ensure_characters_table(conn)
- list_characters(conn)                                         → [(id, name), ...]
- has_any_character(conn)                                       → bool
- get_character_row(conn, character_id)                         → dict | None
- save_tokens(conn, access, refresh, expires_in, char_id, name) → upsert
- get_valid_token(conn, character_id)                           → str | None  (auto-refresh)
- delete_character(conn, character_id)
- update_corporation_id(conn, character_id, corp_id)
- get_client_id() / save_client_id(...)                         → JSON file
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import httpx

_APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.join(
    os.path.dirname(__file__), "..", ".."
)
CONFIG_PATH = os.path.join(_APP_DIR, ".eve_config.json")
TOKEN_ENDPOINT = "https://login.eveonline.com/v2/oauth/token"
_DEFAULT_CLIENT_ID = "50cc73daf13d4109a06821c143cb5ca4"


# ---------------------------------------------------------------------------
# JSON config (client_id only)
# ---------------------------------------------------------------------------

def _load_json() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(data: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def get_client_id() -> str | None:
    return _load_json().get("client_id") or _DEFAULT_CLIENT_ID


def save_client_id(client_id: str) -> None:
    data = _load_json()
    data["client_id"] = client_id
    _save_json(data)


# ---------------------------------------------------------------------------
# DB schema + migration from legacy .eve_config.json
# ---------------------------------------------------------------------------

def ensure_characters_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            character_id     INTEGER PRIMARY KEY,
            character_name   TEXT    NOT NULL,
            refresh_token    TEXT    NOT NULL,
            access_token     TEXT,
            token_expires_at REAL,
            corporation_id   INTEGER,
            last_sync_at     REAL,
            added_at         REAL    NOT NULL
        )
    """)
    conn.commit()
    _migrate_legacy_json(conn)


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    """One-time migration of single-character .eve_config.json to characters table."""
    data = _load_json()
    char_id = data.get("character_id")
    refresh = data.get("refresh_token")
    if not (char_id and refresh):
        return  # nothing to migrate

    # Migrate only if this char isn't already in DB
    existing = conn.execute(
        "SELECT 1 FROM characters WHERE character_id=?", (int(char_id),)
    ).fetchone()
    if existing:
        _strip_token_fields(data)
        return

    conn.execute(
        """INSERT INTO characters
           (character_id, character_name, refresh_token, access_token,
            token_expires_at, added_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            int(char_id),
            data.get("character_name", "Unknown"),
            refresh,
            data.get("access_token"),
            data.get("token_expires_at"),
            time.time(),
        ),
    )
    conn.commit()
    _strip_token_fields(data)


def _strip_token_fields(data: dict) -> None:
    """Remove migrated per-char fields from .eve_config.json (keep client_id)."""
    changed = False
    for k in ("access_token", "refresh_token", "token_expires_at",
              "character_id", "character_name"):
        if k in data:
            del data[k]
            changed = True
    if changed:
        _save_json(data)


# ---------------------------------------------------------------------------
# Character CRUD
# ---------------------------------------------------------------------------

def list_characters(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT character_id, character_name FROM characters ORDER BY added_at ASC"
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def has_any_character(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM characters LIMIT 1").fetchone()
    return row is not None


def get_character_row(conn: sqlite3.Connection, character_id: int) -> dict | None:
    row = conn.execute(
        """SELECT character_id, character_name, refresh_token, access_token,
                  token_expires_at, corporation_id, last_sync_at, added_at
           FROM characters WHERE character_id=?""",
        (int(character_id),),
    ).fetchone()
    if not row:
        return None
    return {
        "character_id":     int(row[0]),
        "character_name":   row[1],
        "refresh_token":    row[2],
        "access_token":     row[3],
        "token_expires_at": row[4],
        "corporation_id":   row[5],
        "last_sync_at":     row[6],
        "added_at":         row[7],
    }


def save_tokens(
    conn: sqlite3.Connection,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    character_id: int,
    character_name: str,
) -> None:
    """Upsert character + tokens."""
    expires_at = time.time() + expires_in - 60
    conn.execute(
        """INSERT INTO characters
           (character_id, character_name, refresh_token, access_token,
            token_expires_at, added_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(character_id) DO UPDATE SET
             character_name   = excluded.character_name,
             refresh_token    = excluded.refresh_token,
             access_token     = excluded.access_token,
             token_expires_at = excluded.token_expires_at""",
        (
            int(character_id), character_name, refresh_token, access_token,
            expires_at, time.time(),
        ),
    )
    conn.commit()


def delete_character(conn: sqlite3.Connection, character_id: int) -> None:
    conn.execute("DELETE FROM characters WHERE character_id=?", (int(character_id),))
    # Cascade-clean per-char cache rows
    for tbl, col in (
        ("char_blueprints_cache", "character_id"),
        ("char_assets_cache",     "character_id"),
        ("char_skills_cache",     "character_id"),
    ):
        try:
            conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (int(character_id),))
        except sqlite3.OperationalError:
            pass
    conn.commit()


def update_corporation_id(
    conn: sqlite3.Connection, character_id: int, corp_id: int
) -> None:
    conn.execute(
        "UPDATE characters SET corporation_id=? WHERE character_id=?",
        (int(corp_id), int(character_id)),
    )
    conn.commit()


def update_last_sync(conn: sqlite3.Connection, character_id: int) -> None:
    conn.execute(
        "UPDATE characters SET last_sync_at=? WHERE character_id=?",
        (time.time(), int(character_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Token retrieval / refresh
# ---------------------------------------------------------------------------

def get_valid_token(conn: sqlite3.Connection, character_id: int) -> str | None:
    """Vrátí platný access_token pro daný char — auto-refresh při expiraci."""
    row = get_character_row(conn, character_id)
    if not row:
        return None

    access = row["access_token"]
    refresh = row["refresh_token"]
    expires = row["token_expires_at"] or 0

    if access and time.time() < expires:
        return access

    client_id = get_client_id()
    if not client_id or not refresh:
        return None

    r = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh,
            "client_id":     client_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200:
        return None

    resp = r.json()
    new_access = resp["access_token"]
    new_refresh = resp.get("refresh_token", refresh)
    new_expires_at = time.time() + resp.get("expires_in", 1200) - 60

    conn.execute(
        """UPDATE characters
           SET access_token=?, refresh_token=?, token_expires_at=?
           WHERE character_id=?""",
        (new_access, new_refresh, new_expires_at, int(character_id)),
    )
    conn.commit()
    return new_access
