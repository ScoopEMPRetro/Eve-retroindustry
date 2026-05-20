"""
Ukládání a načítání ESI OAuth tokenů do JSON souboru.
Refresh token je trvalý, access token expiruje po 20 minutách.
"""
import json
import os
import time
import httpx

_APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.join(os.path.dirname(__file__), "../../..")
CONFIG_PATH = os.path.join(_APP_DIR, ".eve_config.json")
TOKEN_ENDPOINT = "https://login.eveonline.com/v2/oauth/token"


def _load() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save(data: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)  # jen pro majitele


def get_client_id() -> str | None:
    return _load().get("client_id")


def save_client_id(client_id: str):
    data = _load()
    data["client_id"] = client_id
    _save(data)


def save_tokens(access_token: str, refresh_token: str, expires_in: int,
                character_id: int, character_name: str):
    data = _load()
    data.update({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": time.time() + expires_in - 60,  # 60s buffer
        "character_id": character_id,
        "character_name": character_name,
    })
    _save(data)


def get_character() -> tuple[int, str] | None:
    data = _load()
    cid = data.get("character_id")
    cname = data.get("character_name")
    if cid and cname:
        return int(cid), cname
    return None


def get_valid_token() -> str | None:
    """Vrátí platný access token — automaticky refreshuje pokud expiroval."""
    data = _load()
    access  = data.get("access_token")
    refresh = data.get("refresh_token")
    expires = data.get("token_expires_at", 0)
    client_id = data.get("client_id")

    if not access or not refresh:
        return None

    if time.time() < expires:
        return access

    # Refresh
    if not client_id:
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
    data["access_token"]    = resp["access_token"]
    data["token_expires_at"] = time.time() + resp.get("expires_in", 1200) - 60
    if "refresh_token" in resp:
        data["refresh_token"] = resp["refresh_token"]
    _save(data)
    return data["access_token"]


def is_logged_in() -> bool:
    return get_valid_token() is not None
