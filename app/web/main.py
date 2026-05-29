"""FastAPI web aplikace pro EVE Retroindustry."""
from __future__ import annotations

APP_VERSION = "0.4.10"

import asyncio
import datetime
import os
import json
import sqlite3
import sys as _sys
import threading
import time as _time
import zipfile as _zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.auth.token_store import (
    ensure_characters_table,
    list_characters,
    has_any_character,
    get_character_row,
    get_valid_token as _get_valid_token_for,
    delete_character,
    update_corporation_id,
    update_last_sync,
)
from app.auth.esi_oauth import start_web_login, cancel_web_login
from app.character.blueprints import fetch_blueprints, ensure_bp_table
from app.character.assets import (
    fetch_assets, ensure_assets_table, assets_at_location,
    fetch_corp_assets, ensure_corp_assets_table,
)
from app.db.type_resolver import resolve_names_bulk
from app.esi.client import search_type_by_name
from app.cache.blueprint_cache import resolve_type
from app.db.database import get_session
from app.manufacturing.planner import build_plan, find_blueprint_for_product, calc_job_time, format_duration
from app.bom.resolver import BOMResolver
from app.market.prices import ensure_price_table, fetch_station_volumes, get_cached_station_volumes, fetch_structure_market
from app.web.prices_helper import (
    get_prices_for_ids,
    get_price_cache_stats,
    refresh_jita_prices_all,
    get_all_price_items,
    set_custom_price,
    stream_jita_refresh,
)
from app.web.location_resolver import (
    resolve_station_names_bulk,
    ensure_location_name_table,
    load_location_names_from_db,
    locations_in_system,
    get_region_for_location,
    get_security_status,
)
from app.web.industry_helper import (
    ensure_industry_tables,
    get_adjusted_prices,
    get_sci_for_system,
    get_station_me_bonus,
    save_station_me_bonus,
    get_station_te_multiplier,
    get_station_me_bonus_pct,
    get_station_me_multiplier,
    get_station_facility,
    get_product_te_multiplier,
    get_station_cost_bonus,
    populate_rig_bonuses,
    get_rig_types,
    save_station_rigs_full,
    get_station_rigs_full,
    _SCC,
)
from app.character.skills import (
    ensure_skills_table,
    fetch_skills,
    get_cached_skills,
    get_mfg_skill_ids,
)
from app.web.projects_helper import (
    ensure_project_tables,
    list_projects,
    create_project,
    add_plan_to_project,
    get_project_detail,
)

# Path resolution — works in dev mode and when frozen by PyInstaller.
# launcher.py sets EVE_APP_DIR / EVE_BUNDLE_DIR before importing this module.
_APP_DIR = os.environ.get("EVE_APP_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_BUNDLE_DIR = os.environ.get("EVE_BUNDLE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

DB_ABS = os.path.join(_APP_DIR, "eve_cache.db")
TEMPLATES_DIR = Path(_BUNDLE_DIR) / "app" / "web" / "templates"

SDE_DOWNLOAD_URL = (
    "https://github.com/ScoopEMPRetro/Eve-retroindustry"
    "/releases/latest/download/sde_base.db"
)

# Set to True once SDE tables are confirmed present. Guards the setup gate.
_SDE_READY: list[bool] = [False]

# Tracks post-login ESI sync state.
_sync_state: dict = {"running": False, "done": False}

app = FastAPI(title="EVE Retroindustry")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception):
    """Log every uncaught exception with traceback so console=False bundles can be debugged."""
    import traceback
    from fastapi.responses import PlainTextResponse
    tb = traceback.format_exc()
    print(f"[error] {request.method} {request.url.path} -> {type(exc).__name__}: {exc}\n{tb}",
          flush=True)
    return PlainTextResponse(f"Internal Server Error\n\n{type(exc).__name__}: {exc}\n\n{tb}",
                             status_code=500)


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    """Redirect every request to /setup until SDE data is available."""
    if not _SDE_READY[0] and not request.url.path.startswith("/setup"):
        return RedirectResponse("/setup")
    return await call_next(request)


_SDE_TABLES_TO_REFRESH = (
    "sde_types",
    "sde_groups",
    "sde_blueprints",
    "sde_blueprint_materials",
    "sde_blueprint_products",
    "sde_blueprint_skills",
    "sde_skill_time_bonus",
)


def _bundled_sde_path() -> str | None:
    """Vrátí cestu k sde_base.db bundlované v PyInstaller balíku, nebo None.

    Bundle dir = sys._MEIPASS (frozen) / projekt root (dev). V dev módu
    sde_base.db leží přímo v rootu projektu.
    """
    candidate = os.path.join(_BUNDLE_DIR, "sde_base.db")
    return candidate if os.path.isfile(candidate) else None


def _refresh_sde_from_bundle(conn: sqlite3.Connection) -> int:
    """Pokud bundlovaná sde_base.db má víc typů než user's eve_cache.db,
    nahradí SDE tabulky čerstvými daty. Vrátí počet typů PO refreshi
    (0 = nic se nestalo).

    User data (characters, BP cache, prices, projekty, …) zůstává — měníme
    jen tabulky z `_SDE_TABLES_TO_REFRESH`.
    """
    bundled = _bundled_sde_path()
    if not bundled:
        return 0

    user_count = conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
    bsrc = sqlite3.connect(bundled)
    try:
        bundled_count = bsrc.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
    finally:
        bsrc.close()

    if bundled_count <= user_count:
        return user_count  # user už má aspoň tolik typů → ne-merge

    print(f"[sde] refreshing SDE tables: user={user_count}, bundled={bundled_count}",
          flush=True)
    # Attach bundlovanou DB jako 'src' a kopíruj tabulky.
    conn.execute("ATTACH DATABASE ? AS src", (bundled,))
    try:
        for table in _SDE_TABLES_TO_REFRESH:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.execute(f"CREATE TABLE {table} AS SELECT * FROM src.{table}")
            except sqlite3.Error as exc:
                print(f"[sde] table {table}: {exc}", flush=True)
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")
    return bundled_count


@app.on_event("startup")
async def _startup_populate_groups():
    """Check SDE readiness, refresh from bundled DB if outdated, then
    load group names and rig bonuses."""
    # Fresh install — pokud eve_cache.db neexistuje a máme bundlovaný SDE,
    # zkopírujeme rovnou (bypassuje stará /setup/download stránka).
    try:
        if not os.path.exists(DB_ABS):
            bundled = _bundled_sde_path()
            if bundled:
                import shutil
                shutil.copy2(bundled, DB_ABS)
                print(f"[sde] fresh install — copied bundled SDE to {DB_ABS}",
                      flush=True)
    except Exception as exc:
        print(f"[sde] fresh-install copy failed: {exc}", flush=True)

    try:
        conn = get_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
        except sqlite3.OperationalError:
            count = 0

        if count > 0:
            try:
                count = _refresh_sde_from_bundle(conn) or count
            except Exception as exc:
                print(f"[sde] refresh failed: {exc}", flush=True)

        _SDE_READY[0] = count > 0
        if _SDE_READY[0]:
            populate_rig_bonuses(conn)
            await _ensure_groups_populated(conn)
        conn.close()
    except Exception:
        _SDE_READY[0] = False


def _isk(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.2f}".replace(",", " ")


def _format_number(v) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _format_date(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(v)).strftime('%d.%m.%Y %H:%M')
    except Exception:
        return str(v)


def _ts_ago(ts: float) -> str:
    """Human-readable relative time from Unix timestamp."""
    try:
        delta = int(_time.time() - float(ts))
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def _ts_to_str(ts: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ""


templates.env.filters["isk"] = _isk
templates.env.filters["format_number"] = _format_number
templates.env.filters["format_date"] = _format_date
templates.env.filters["ts_ago"] = _ts_ago
templates.env.filters["ts_to_str"] = _ts_to_str


def _tr(name: str, request: Request, context: dict) -> HTMLResponse:
    """Starlette nové API: request jako první argument."""
    conn = get_conn()
    try:
        active = get_active_character(request, conn)
        all_chars = list_characters(conn)
    finally:
        conn.close()
    context.setdefault("character", active)
    context.setdefault("all_characters", all_chars)
    context.setdefault("active_char_id", active[0] if active else None)
    return templates.TemplateResponse(request, name, context)


# ---------------------------------------------------------------------------
# First-run setup routes
# ---------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return _tr("setup.html", request, {"sde_url": SDE_DOWNLOAD_URL})


@app.get("/setup/download")
async def setup_download():
    """SSE stream: downloads sde_base.db, writes to eve_cache.db, sets _SDE_READY."""

    async def _stream():
        tmp_path = DB_ABS + ".download"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", SDE_DOWNLOAD_URL) as r:
                    if r.status_code != 200:
                        yield f"data: {json.dumps({'error': f'HTTP {r.status_code}'})}\n\n"
                        return
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            pct = int(downloaded * 100 / total) if total else 0
                            yield f"data: {json.dumps({'downloaded': downloaded, 'total': total, 'pct': pct})}\n\n"

            import shutil
            # Dispose pooled SQLAlchemy connections BEFORE the move — otherwise
            # they hold an open file descriptor on the empty placeholder DB and
            # subsequent INSERTs fail with SQLITE_READONLY_DBMOVED ("attempt to
            # write a readonly database").
            from app.db.database import engine as _alchemy_engine
            _alchemy_engine.dispose()

            shutil.move(tmp_path, DB_ABS)

            # Re-run startup population now that SDE is available
            _SDE_READY[0] = True
            conn = get_conn()
            try:
                populate_rig_bonuses(conn)
                await _ensure_groups_populated(conn)
            except Exception:
                pass
            finally:
                conn.close()

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as exc:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_ABS)
    ensure_bp_table(conn)
    ensure_assets_table(conn)
    ensure_corp_assets_table(conn)
    ensure_skills_table(conn)
    ensure_price_table(conn)
    ensure_location_name_table(conn)
    ensure_industry_tables(conn)
    ensure_project_tables(conn)
    ensure_characters_table(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sde_groups (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS char_wallet_cache (
            character_id INTEGER PRIMARY KEY,
            balance      REAL NOT NULL,
            cached_at    REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


_WALLET_CACHE_TTL = 300.0  # 5 minutes


async def _fetch_wallet_balance(
    conn: sqlite3.Connection, char_id: int, token: str | None
) -> float | None:
    """Returns ISK wallet balance, using a 5-min SQLite cache."""
    now = _time.time()
    row = conn.execute(
        "SELECT balance, cached_at FROM char_wallet_cache WHERE character_id=?", (char_id,)
    ).fetchone()
    if row and (now - row[1]) < _WALLET_CACHE_TTL:
        return row[0]
    if not token:
        return row[0] if row else None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://esi.evetech.net/latest/characters/{char_id}/wallet/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                balance = float(r.json())
                conn.execute(
                    "INSERT OR REPLACE INTO char_wallet_cache (character_id, balance, cached_at) VALUES (?,?,?)",
                    (char_id, balance, now),
                )
                conn.commit()
                return balance
    except Exception:
        pass
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Active character helpers (cookie-based)
# ---------------------------------------------------------------------------

ACTIVE_COOKIE = "active_char"


def get_active_character_id(request: Request, conn: sqlite3.Connection | None = None) -> int | None:
    """Return the active character id from cookie, or fall back to first char in DB."""
    cookie = request.cookies.get(ACTIVE_COOKIE) if request else None
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if cookie:
            try:
                cid = int(cookie)
            except ValueError:
                cid = None
            if cid and get_character_row(conn, cid):
                return cid
        chars = list_characters(conn)
        return chars[0][0] if chars else None
    finally:
        if own_conn:
            conn.close()


def get_active_character(request: Request, conn: sqlite3.Connection | None = None) -> tuple[int, str] | None:
    """Return (char_id, char_name) for the active character, or None."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        cid = get_active_character_id(request, conn)
        if cid is None:
            return None
        row = get_character_row(conn, cid)
        if row:
            return (row["character_id"], row["character_name"])
        return None
    finally:
        if own_conn:
            conn.close()


def get_active_token(request: Request, conn: sqlite3.Connection | None = None) -> str | None:
    """Return a fresh access token for the active character."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        cid = get_active_character_id(request, conn)
        if cid is None:
            return None
        return _get_valid_token_for(conn, cid)
    finally:
        if own_conn:
            conn.close()


def get_token_for(character_id: int, conn: sqlite3.Connection | None = None) -> str | None:
    """Return a fresh access token for a specific character."""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        return _get_valid_token_for(conn, character_id)
    finally:
        if own_conn:
            conn.close()


def _science_skill_mult(
    conn: sqlite3.Connection,
    bp_type_id: int,
    activity: str,
    skills: dict[int, int],
) -> tuple[float, list[tuple[str, int, float, int]]]:
    """Vrátí (multiplier, [(skill_name, char_level, bonus_pct, required_level), ...]).

    Každý required skill s time bonusem přispívá (1 - level * bonus_pct/100).
    Industry a AdvIndustry jsou zpracovány zvlášť — zde je přeskakujeme.
    """
    try:
        rows = conn.execute(
            """SELECT bs.skill_type_id,
                      COALESCE(st.skill_name, t.name) AS skill_name,
                      bs.required_level,
                      st.time_bonus_pct
               FROM sde_blueprint_skills bs
               LEFT JOIN sde_skill_time_bonus st ON st.skill_type_id = bs.skill_type_id
               LEFT JOIN sde_types t              ON t.type_id       = bs.skill_type_id
               WHERE bs.blueprint_type_id = ? AND bs.activity = ?
                 AND bs.skill_type_id NOT IN (3380, 3388)""",
            (bp_type_id, activity),
        ).fetchall()
    except Exception:
        return 1.0, []

    mult = 1.0
    details: list[tuple[str, int, float, int]] = []
    for skill_id, skill_name, req_level, bonus_pct in rows:
        level = skills.get(skill_id, 0)
        if bonus_pct is not None:
            mult *= 1.0 - level * bonus_pct / 100
        details.append((skill_name or f"Skill {skill_id}", level, float(bonus_pct or 0), int(req_level)))
    return max(0.01, mult), details


async def _ensure_groups_populated(conn: sqlite3.Connection) -> None:
    """Populate sde_groups via ESI /universe/groups/{id}/ with concurrency limit."""
    if conn.execute("SELECT COUNT(*) FROM sde_groups").fetchone()[0] > 0:
        return
    group_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT group_id FROM sde_types WHERE group_id > 0 AND published = 1"
    ).fetchall()]
    if not group_ids:
        return

    sem = asyncio.Semaphore(50)

    async def _fetch(client: httpx.AsyncClient, gid: int):
        async with sem:
            try:
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/groups/{gid}/",
                    params={"datasource": "tranquility"},
                    timeout=10,
                )
                if r.status_code == 200:
                    d = r.json()
                    if d.get("published", True):
                        return (gid, d["name"])
            except Exception:
                pass
            return None

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch(client, gid) for gid in group_ids])

    for row in results:
        if row:
            conn.execute("INSERT OR REPLACE INTO sde_groups VALUES (?,?)", row)
    conn.commit()


def _collect_type_ids(node) -> list[int]:
    ids = [node.type_id]
    for child in node.children:
        ids.extend(_collect_type_ids(child))
    return ids


def _is_real_location(loc_id: int) -> bool:
    """Vrátí True pokud je ID skutečná stanice/struktura, ne item_id kontejneru/lodi."""
    # NPC stanice: 60_000_000 – 64_000_000
    # Player struktury: > 1_000_000_000_000
    # Solární systémy: 30_000_000 – 34_000_000 (věci ve vesmíru)
    # Item_id lodí/kontejnerů: typicky miliardová čísla ale < 1 bilion
    if 60_000_000 <= loc_id < 64_000_000:
        return True
    if loc_id > 1_000_000_000_000:
        return True
    if 30_000_000 <= loc_id < 34_000_000:
        return True  # sluneční soustava — věci v prostoru
    return False


def _resolve_root_locations(assets: list) -> dict[int, int]:
    """
    Vrátí {item_id: root_location_id} kde root_location_id je skutečná stanice/struktura.
    Prochází řetězec item_id → location_id dokud nedosáhne reálné lokace.
    """
    # Mapa item_id → location_id pro rychlé hledání rodiče
    parent: dict[int, int] = {a.item_id: a.location_id for a in assets}

    result: dict[int, int] = {}
    for a in assets:
        loc = a.location_id
        seen: set[int] = set()
        while not _is_real_location(loc) and loc in parent and loc not in seen:
            seen.add(loc)
            loc = parent[loc]
        result[a.item_id] = loc
    return result


def _load_blueprints_from_cache(conn: sqlite3.Connection, char_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT data_json FROM char_blueprints_cache WHERE character_id=?", (char_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


def _load_assets_from_cache(conn: sqlite3.Connection, char_id: int) -> list[dict]:
    """Načte assety přímo z JSON cache bez ESI volání."""
    row = conn.execute(
        "SELECT data_json FROM char_assets_cache WHERE character_id=?", (char_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


def _load_corp_assets_from_cache(conn: sqlite3.Connection, corp_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT data_json FROM corp_assets_cache WHERE corporation_id=?", (corp_id,)
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0])


_CORP_DIV_LABEL: dict[str, str] = {
    "CorpSAG1": "Division 1",
    "CorpSAG2": "Division 2",
    "CorpSAG3": "Division 3",
    "CorpSAG4": "Division 4",
    "CorpSAG5": "Division 5",
    "CorpSAG6": "Division 6",
    "CorpSAG7": "Division 7",
    "Hangar": "Hangar",
    "CorpDeliveries": "Deliveries",
}
_CORP_DIV_ORDER = list(_CORP_DIV_LABEL.keys())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _open_in_external_browser(url: str) -> bool:
    """Otevře URL v systémovém default browseru bez toho aby
    zdědil AppImage / PyInstaller env (LD_LIBRARY_PATH, QT_*…),
    který by jinak crashnul Firefox/Chrome (snažili by se loadnout
    naše bundlované Qt libs). Vrátí True pokud se daný spawn povedl.

    AppImage runtime ukládá originální hodnoty do `APPIMAGE_ORIGINAL_*`
    a PyInstaller bootloader do `_PYI_*` — vrátíme je tam zpět než
    voláme xdg-open.
    """
    import subprocess
    if _sys.platform.startswith("win"):
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            print(f"[browser] os.startfile failed: {exc}", flush=True)
            return False
    if _sys.platform == "darwin":
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:
            print(f"[browser] open failed: {exc}", flush=True)
            return False

    # Linux: restore the env that existed before AppImage / PyInstaller
    # took over, so the spawned browser doesn't try to load our bundled
    # libs.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LD_LIBRARY_PATH", "QT_", "QML",
                                "GST_", "GTK_", "PYTHON", "_PYI_"))}
    for k in list(os.environ.keys()):
        if k.startswith("APPIMAGE_ORIGINAL_"):
            env[k[len("APPIMAGE_ORIGINAL_"):]] = os.environ[k]
            env.pop(k, None)
    for cmd in (["xdg-open", url], ["x-www-browser", url],
                ["firefox", url], ["google-chrome", url],
                ["chromium", url]):
        try:
            subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            print(f"[browser] spawned {cmd[0]} for SSO login", flush=True)
            return True
        except FileNotFoundError:
            continue
        except Exception as exc:
            print(f"[browser] {cmd[0]} failed: {exc}", flush=True)
            continue
    return False


@app.get("/auth/login")
async def auth_login(request: Request):
    """Spustí OAuth flow + pokusí se otevřít EVE SSO v systémovém
    default browseru. Webview ukáže waiting page s Cancel buttonem.

    Pokud spawn external browseru selže, waiting page má taky
    "Open in this window" fallback link (webview se naviguje na SSO).
    """
    _sync_state["done"] = False
    url = start_web_login()
    if not url:
        return RedirectResponse("/?login_busy=1")
    opened = _open_in_external_browser(url)
    return _tr("auth_waiting.html", request, {
        "auth_url": url,
        "external_opened": opened,
    })


@app.post("/auth/cancel")
async def auth_cancel():
    """Zruší probíhající login. Server shutdown + lock release."""
    cancelled = cancel_web_login()
    return {"cancelled": cancelled}


@app.get("/api/auth/status")
async def api_auth_status():
    """Polling endpoint pro waiting page. Vrací stav login flow."""
    from app.auth.esi_oauth import _login_lock
    conn = get_conn()
    try:
        has_chars = has_any_character(conn)
    finally:
        conn.close()
    # Pokud lock není acquired → login flow skončil (success nebo cancel).
    # has_chars rozlišuje úspěch (uloženy tokeny) vs cancel/error.
    in_progress = _login_lock.locked()
    return {"in_progress": in_progress, "has_character": has_chars}


async def _bg_initial_sync():
    """Fetch blueprints + personal + corp assets from ESI for every known char."""
    conn = None
    try:
        conn = get_conn()
        chars = list_characters(conn)
        if not chars:
            return

        all_loc_ids: set[int] = set()
        any_token: str | None = None

        async with httpx.AsyncClient() as client:
            for char_id, _name in chars:
                try:
                    token = _get_valid_token_for(conn, char_id)
                except Exception as exc:
                    print(f"[sync] token refresh failed for {char_id}: {exc}", flush=True)
                    continue
                if not token:
                    continue
                any_token = token
                try:
                    await fetch_blueprints(client, char_id, token, conn)
                    personal = await fetch_assets(client, char_id, token, conn)
                    await fetch_skills(client, char_id, token, conn)
                    try:
                        corp_id, corp = await fetch_corp_assets(client, char_id, token, conn)
                        if corp_id:
                            update_corporation_id(conn, char_id, corp_id)
                    except Exception as exc:
                        print(f"[sync] corp_assets failed for {char_id}: {exc}", flush=True)
                        corp = []
                    all_loc_ids |= {a.location_id for a in personal}
                    all_loc_ids |= {a.location_id for a in corp}
                    update_last_sync(conn, char_id)
                except Exception as exc:
                    print(f"[sync] character {char_id} sync failed: {exc}", flush=True)
                    continue

        if all_loc_ids and any_token:
            try:
                await resolve_station_names_bulk(list(all_loc_ids), token=any_token, conn=conn)
            except Exception as exc:
                print(f"[sync] resolve_station_names_bulk failed: {exc}", flush=True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[sync] fatal: {exc}", flush=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _sync_state["running"] = False
        _sync_state["done"] = True


@app.get("/auth/sync", response_class=HTMLResponse)
async def auth_sync(request: Request):
    conn = get_conn()
    try:
        if not has_any_character(conn):
            return RedirectResponse("/")
    finally:
        conn.close()
    if not _sync_state["running"] and not _sync_state["done"]:
        _sync_state["running"] = True
        _sync_state["done"] = False
        asyncio.create_task(_bg_initial_sync())
    return _tr("sync.html", request, {})


@app.get("/api/sync-status")
async def api_sync_status():
    return {"done": _sync_state["done"], "running": _sync_state["running"]}


# ---------------------------------------------------------------------------
# Multi-character management endpoints
# ---------------------------------------------------------------------------

@app.post("/api/characters/{char_id}/activate")
async def api_activate_character(char_id: int):
    """Set active_char cookie."""
    from fastapi.responses import JSONResponse
    conn = get_conn()
    try:
        if not get_character_row(conn, char_id):
            return JSONResponse({"ok": False, "error": "Unknown character"}, status_code=404)
    finally:
        conn.close()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(ACTIVE_COOKIE, str(char_id), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.delete("/api/characters/{char_id}")
async def api_delete_character(request: Request, char_id: int):
    """Remove a character (and its cached data)."""
    from fastapi.responses import JSONResponse
    conn = get_conn()
    try:
        delete_character(conn, char_id)
    finally:
        conn.close()
    resp = JSONResponse({"ok": True})
    if request.cookies.get(ACTIVE_COOKIE) == str(char_id):
        resp.delete_cookie(ACTIVE_COOKIE)
    return resp


@app.post("/api/sync/start")
async def api_sync_start():
    """Manually trigger an ESI sync for all characters."""
    if _sync_state["running"]:
        return {"ok": False, "error": "Already running"}
    _sync_state["running"] = True
    _sync_state["done"] = False
    asyncio.create_task(_bg_initial_sync())
    return {"ok": True}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.auth.token_store import get_client_id
    from app.auth.esi_oauth import CALLBACK_URL, SCOPES
    return _tr("settings.html", request, {
        "client_id": get_client_id() or "",
        "callback_url": CALLBACK_URL,
        "scopes": SCOPES,
    })


@app.post("/api/settings/client-id")
async def api_save_client_id(request: Request):
    body = await request.json()
    cid = body.get("client_id", "").strip()
    if not cid:
        return {"ok": False, "error": "Client ID cannot be empty."}
    from app.auth.token_store import save_client_id
    save_client_id(cid)
    return {"ok": True}


# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = get_conn()
    logged_in = has_any_character(conn)
    price_stats = {}
    char_cards: list[dict] = []
    corp_names: dict[int, str] = {}
    agg_bps = agg_assets = agg_locations = 0
    agg_value: float | None = None

    if logged_in:
        chars = list_characters(conn)
        active_char_id = get_active_character_id(request, conn)

        # Resolve corporation names via ESI bulk
        corp_ids = list({
            row["corporation_id"]
            for row in [get_character_row(conn, cid) for cid, _ in chars]
            if row and row.get("corporation_id")
        })
        if corp_ids:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.post(
                        "https://esi.evetech.net/latest/universe/names/",
                        json=corp_ids,
                        headers={"Accept": "application/json"},
                    )
                    if r.status_code == 200:
                        for item in r.json():
                            corp_names[item["id"]] = item["name"]
            except Exception:
                pass

        # Collect prices once for all assets
        # all_assets_by_char: everything incl. singletons — for value calculation
        # assets_by_char: non-singletons only — for location/count display stats
        all_type_ids_set: set[int] = set()
        assets_by_char: dict[int, list[dict]] = {}
        all_assets_by_char: dict[int, list[dict]] = {}
        char_rows: dict[int, dict] = {}
        for cid, _ in chars:
            raw = _load_assets_from_cache(conn, cid)
            all_assets_by_char[cid] = raw
            assets_by_char[cid] = [a for a in raw if not a.get("is_singleton", False)]
            all_type_ids_set.update(a["type_id"] for a in raw)
            char_rows[cid] = get_character_row(conn, cid) or {}

        prices: dict[int, tuple] = {}
        if all_type_ids_set:
            prices = await get_prices_for_ids(conn, list(all_type_ids_set))

        # Blueprint group_ids — exclude from net worth (matches in-game behavior)
        bp_group_ids: set[int] = {
            r[0] for r in conn.execute(
                "SELECT group_id FROM sde_groups WHERE name LIKE '%Blueprint%'"
            ).fetchall()
        }
        type_group: dict[int, int] = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT type_id, group_id FROM sde_types WHERE type_id IN ({','.join('?' * len(all_type_ids_set))})",
                list(all_type_ids_set),
            ).fetchall()
        } if all_type_ids_set else {}

        # Fetch wallet balances concurrently (5-min cache)
        wallet_balances: dict[int, float | None] = dict(
            zip(
                [cid for cid, _ in chars],
                await asyncio.gather(*[
                    _fetch_wallet_balance(conn, cid, _get_valid_token_for(conn, cid))
                    for cid, _ in chars
                ]),
            )
        )

        for cid, cname in chars:
            char_row = char_rows[cid]
            bp_row = conn.execute(
                "SELECT json_array_length(data_json) FROM char_blueprints_cache WHERE character_id=?",
                (cid,),
            ).fetchone()
            bp_count = bp_row[0] if bp_row and bp_row[0] else 0

            assets = assets_by_char.get(cid, [])         # non-singleton, for counts
            all_assets = all_assets_by_char.get(cid, [])  # all items, for value
            locs = {a["location_id"] for a in assets}

            char_value: float | None = None
            # Exclude blueprints from value (matches in-game "Total Net Worth" behavior)
            priced_assets = [
                (a, prices.get(a["type_id"], (None, None))[0])
                for a in all_assets
                if type_group.get(a["type_id"]) not in bp_group_ids
            ]
            priced_sum = sum(p * a.get("quantity", 1) for a, p in priced_assets if p is not None)
            if any(p is not None for _, p in priced_assets):
                char_value = priced_sum

            wallet = wallet_balances.get(cid)
            net_worth: float | None = None
            if char_value is not None or wallet is not None:
                net_worth = (char_value or 0.0) + (wallet or 0.0)

            last_sync_at = char_row.get("last_sync_at")
            corp_id = char_row.get("corporation_id")

            char_cards.append({
                "char_id":     cid,
                "char_name":   cname,
                "corp_id":     corp_id,
                "corp_name":   corp_names.get(corp_id, "") if corp_id else "",
                "bp_count":    bp_count,
                "asset_count": len(assets),
                "asset_locs":  len(locs),
                "asset_value": char_value,
                "wallet":      wallet,
                "net_worth":   net_worth,
                "last_sync_at": last_sync_at,
                "is_active":   cid == active_char_id,
            })

            agg_bps += bp_count
            agg_assets += len(assets)
            agg_locations = len({loc for c in assets_by_char.values() for a in c for loc in [a["location_id"]]})
            if net_worth is not None:
                agg_value = (agg_value or 0.0) + net_worth
            elif char_value is not None:
                agg_value = (agg_value or 0.0) + char_value

        price_stats = get_price_cache_stats(conn)

    conn.close()
    return _tr("index.html", request, {
        "logged_in": logged_in,
        "char_cards": char_cards,
        "agg_bps": agg_bps,
        "agg_assets": agg_assets,
        "agg_locations": agg_locations,
        "agg_value": agg_value,
        "price_stats": price_stats,
        "login_busy": request.query_params.get("login_busy") == "1",
    })


# ---------------------------------------------------------------------------
# Výrobní plán
# ---------------------------------------------------------------------------

@app.get("/plan", response_class=HTMLResponse)
async def plan_form(request: Request, char: str = "", station: str = ""):
    conn = get_conn()
    # Determine which character drives the form (URL ?char= overrides active cookie)
    plan_char_id: int | None = None
    if char.isdigit():
        plan_char_id = int(char)
        if not get_character_row(conn, plan_char_id):
            plan_char_id = None
    if plan_char_id is None:
        plan_char_id = get_active_character_id(request, conn)
    char_row = get_character_row(conn, plan_char_id) if plan_char_id else None
    token = _get_valid_token_for(conn, plan_char_id) if plan_char_id else None

    location_ids = []
    char_skills: dict[int, int] = {}
    if char_row:
        raw = _load_assets_from_cache(conn, char_row["character_id"])
        location_ids = sorted({a["location_id"] for a in raw if not a.get("is_singleton", False)})
        if token:
            async with httpx.AsyncClient() as client:
                char_skills = await fetch_skills(client, char_row["character_id"], token, conn)
        else:
            char_skills = get_cached_skills(conn, char_row["character_id"])
    product_param = request.query_params.get("product", "")
    if product_param.strip().isdigit():
        row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (int(product_param),)).fetchone()
        if row:
            product_param = row[0]
    # Preserve station when switching character
    prefill_station = station.strip() if station.strip().isdigit() else ""
    prefill_station_name = ""
    if prefill_station:
        row = conn.execute(
            "SELECT name FROM location_name_cache WHERE location_id=?", (int(prefill_station),)
        ).fetchone()
        if row:
            prefill_station_name = row[0]
    conn.close()
    return _tr("plan.html", request, {
        "locations": location_ids,
        "result": None,
        "error": None,
        "form_product": product_param,
        "form_station": prefill_station,
        "form_station_name": prefill_station_name,
        "form_industry":     str(char_skills.get(3380, 0)),
        "form_adv_industry": str(char_skills.get(3388, 0)),
        "plan_char_id": plan_char_id,
    })


@app.post("/plan", response_class=HTMLResponse)
async def plan_result(
    request: Request,
    product: str = Form(...),
    station: str = Form(""),
    reaction_station: int = Form(0),
    qty: int = Form(1),
    mode: str = Form("full"),
    form_me: str = Form(""),
    form_te: str = Form(""),
    facility_tax: str = Form("2.5"),
    reaction_facility_tax: str = Form(""),
    facility_me_bonus: str = Form("0"),
    reaction_me_bonus: str = Form("0"),
    selling_station: int = Form(0),
    form_industry: str = Form("0"),
    form_adv_industry: str = Form("0"),
    plan_char_id: str = Form(""),
):
    conn = get_conn()
    error = None
    plan_data = None
    # Resolve plan character from form, fall back to active char.
    plan_char_id_int: int | None = None
    if plan_char_id.strip().isdigit():
        candidate = int(plan_char_id.strip())
        if get_character_row(conn, candidate):
            plan_char_id_int = candidate
    if plan_char_id_int is None:
        plan_char_id_int = get_active_character_id(request, conn)

    # Parse station — friendly error místo 422 (pokud chybí, raise ValueError níže)
    try:
        station = int(station.strip()) if isinstance(station, str) and station.strip() else 0
    except ValueError:
        station = 0

    # Převeď ME/TE na int pokud zadány
    me_override: int | None = int(form_me) if form_me.strip().isdigit() else None
    te_override: int | None = int(form_te) if form_te.strip().isdigit() else None
    # Safe defaults — overwritten inside try block once BP is known
    me: float = float(me_override) if me_override is not None else 0.0
    te: int   = te_override if te_override is not None else 0

    def _clamp_skill(s: str, max_val: int = 5) -> int:
        try:
            return max(0, min(max_val, int(s.strip())))
        except (ValueError, AttributeError):
            return 0

    industry_level     = _clamp_skill(form_industry)
    adv_industry_level = _clamp_skill(form_adv_industry)

    def _parse_pct(s: str) -> float:
        try:
            return max(0.0, min(25.0, float(s.replace(",", "."))))
        except (ValueError, AttributeError):
            return 0.0

    # facility_me_bonus / reaction_me_bonus z formuláře jsou už jen pro display
    # (form_facility_me_bonus předáno zpět do šablony). Skutečný ME multiplikátor
    # se počítá ze station_rigs v get_station_me_multiplier.

    try:
        if plan_char_id_int is None:
            raise ValueError("Nejsi přihlášen.")
        token = _get_valid_token_for(conn, plan_char_id_int)
        row = get_character_row(conn, plan_char_id_int)
        if not token or not row:
            raise ValueError("Nejsi přihlášen.")
        if not station:
            raise ValueError("Vyber výrobní stanici.")
        char = (row["character_id"], row["character_name"])
        char_id, _ = char

        async with httpx.AsyncClient() as client:
            session = get_session()
            if product.strip().isdigit():
                type_id = int(product.strip())
                type_name = await resolve_type(client, session, type_id)
            else:
                results = await search_type_by_name(client, product.strip())
                if not results:
                    raise ValueError(f"Produkt '{product}' nenalezen.")
                type_id = results[0]
                type_name = await resolve_type(client, session, type_id)
            session.close()

        async with httpx.AsyncClient() as client:
            blueprints, all_assets, char_skills = await asyncio.gather(
                fetch_blueprints(client, char_id, token, conn),
                fetch_assets(client, char_id, token, conn),
                fetch_skills(client, char_id, token, conn),
            )

        # Industry/AdvIndustry vždy z aktuálních char_skills (form_industry pole
        # je hidden a může pocházet ze starého characteru při přepnutí).
        industry_level     = max(0, min(5, int(char_skills.get(3380, 0))))
        adv_industry_level = max(0, min(5, int(char_skills.get(3388, 0))))
        form_industry      = str(industry_level)
        form_adv_industry  = str(adv_industry_level)

        available = assets_at_location(all_assets, station)

        bp = find_blueprint_for_product(blueprints, type_id, conn)
        me = float(me_override if me_override is not None else (bp.material_efficiency if bp else 0))
        te = int(te_override if te_override is not None else (bp.time_efficiency if bp else 0))

        # ME multiplikátor stanice — per-product (rig se aplikuje jen na produkty
        # odpovídající jeho kategorii: Ship rig na lodě, Equipment rig na moduly atd.).
        eff_rxn_station_for_me = reaction_station if reaction_station else station
        mfg_facility = get_station_facility(conn, station)
        rxn_facility = get_station_facility(conn, eff_rxn_station_for_me)
        # Agregované úspory ROOT produktu (pro display)
        mfg_me_mult = get_station_me_multiplier(conn, station)
        rxn_me_mult = get_station_me_multiplier(conn, eff_rxn_station_for_me)

        # Resolver dostane všechny blueprinty postavy → per-product ME se lookup-uje
        # pro každý mezikrok zvlášť (Capital Armor Plates ME může být jiné než root ME).
        resolver = BOMResolver(DB_ABS, blueprints=blueprints)
        root = resolver.resolve(type_id, qty, me=me,
                                mfg_facility=mfg_facility,
                                rxn_facility=rxn_facility)
        resolver.close()

        all_ids = list(set(_collect_type_ids(root) + [type_id]))
        prices = await get_prices_for_ids(conn, all_ids)

        plan = build_plan(
            product_type_id=type_id,
            quantity=qty,
            location_id=station,
            available_assets=available,
            blueprints=blueprints,
            db_path=DB_ABS,
            mode=mode,
            prices=prices,
            mfg_facility=mfg_facility,
            rxn_facility=rxn_facility,
        )
        plan_data = _plan_to_dict(plan, prices, type_name)
        # Přepis ME/TE v plan_data pokud bylo zadáno ručně
        if plan_data.get("blueprint"):
            plan_data["blueprint"]["me"] = int(me)
            plan_data["blueprint"]["te"] = te
        elif me_override is not None:
            plan_data["blueprint"] = {"kind": "—", "me": int(me), "te": te, "runs": "—", "manual": True}
        plan_data["manufacturing_steps"] = _build_manufacturing_steps(root, prices, available)

        # === Výrobní poplatky ===
        def _safe_pct(s: str, default: float) -> float:
            try:
                return float(s.replace(",", "."))
            except (ValueError, AttributeError):
                return default

        fac_tax_pct  = _safe_pct(facility_tax, 2.5)
        fac_tax_rate = fac_tax_pct / 100

        # Reakční stanice — 0 znamená použít stejnou jako výrobní
        eff_rxn_station = reaction_station if reaction_station else station
        sep_rxn_station = eff_rxn_station != station

        rxn_fac_tax_pct  = _safe_pct(reaction_facility_tax, fac_tax_pct) if reaction_facility_tax.strip() else fac_tax_pct
        rxn_fac_tax_rate = rxn_fac_tax_pct / 100

        # Solar system ID výrobní stanice
        sys_row = conn.execute(
            "SELECT solar_system_id FROM location_name_cache WHERE location_id=?", (station,)
        ).fetchone()
        solar_system_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

        # Solar system ID reakční stanice
        if sep_rxn_station:
            rxn_sys_row = conn.execute(
                "SELECT solar_system_id FROM location_name_cache WHERE location_id=?", (eff_rxn_station,)
            ).fetchone()
            rxn_solar_system_id: int | None = rxn_sys_row[0] if rxn_sys_row and rxn_sys_row[0] else None
        else:
            rxn_solar_system_id = solar_system_id

        adj_prices = await get_adjusted_prices(conn)

        mfg_sci = await get_sci_for_system(conn, solar_system_id, "manufacturing") if solar_system_id else 0.0
        rxn_sci = await get_sci_for_system(conn, rxn_solar_system_id, "reaction") if rxn_solar_system_id else 0.0

        # TE multiplikátory pro stanice (struktura + rigy)
        mfg_te_mult = get_station_te_multiplier(conn, station)
        rxn_te_mult = get_station_te_multiplier(conn, eff_rxn_station) if sep_rxn_station else mfg_te_mult

        # Cost bonus na SCI (Raitaru −3 %, Azbel −4 %, Sotiyo −5 %)
        mfg_cost_bonus = get_station_cost_bonus(conn, station)
        rxn_cost_bonus = get_station_cost_bonus(conn, eff_rxn_station) if sep_rxn_station else mfg_cost_bonus

        total_job_fee = 0.0
        total_mfg_time_s = 0   # čas všech výrobních kroků (sekvenčně)
        total_rxn_time_s = 0
        for step in plan_data["manufacturing_steps"]:
            step_mfg_time = 0
            step_rxn_time = 0
            # V "components" módu kupujeme 1. úroveň z trhu — instalační poplatky
            # platíme jen za finální job (sestavení produktu samotného).
            skip_fee = (mode == "components" and not step.get("is_final"))
            for job in step["jobs"]:
                is_rxn   = job.get("activity") == "reaction"
                sci      = rxn_sci      if is_rxn else mfg_sci
                tax_rate = rxn_fac_tax_rate if is_rxn else fac_tax_rate
                cost_bonus = rxn_cost_bonus if is_rxn else mfg_cost_bonus

                # EIV musí používat BASE množství ze SDE (ne ME-redukovaná)
                bp_id = job.get("blueprint_type_id")
                runs  = job.get("runs", 1) or 1
                if bp_id:
                    base_mats = conn.execute(
                        "SELECT material_type_id, quantity FROM sde_blueprint_materials"
                        " WHERE blueprint_type_id=? AND activity=?",
                        (bp_id, job.get("activity", "manufacturing")),
                    ).fetchall()
                    eiv = sum(adj_prices.get(m[0], 0.0) * m[1] * runs for m in base_mats)
                else:
                    eiv = sum(adj_prices.get(inp["type_id"], 0.0) * inp["quantity"]
                              for inp in job["inputs"])

                # CCP formula: TIF = EIV × ((SCI × (1 - structure_cost_bonus)) + tax + SCC)
                job_fee = eiv * (sci * (1.0 - cost_bonus) + tax_rate + _SCC)
                job["eiv"] = eiv
                job["sci"] = sci
                job["tax_pct"] = round(tax_rate * 100, 2)
                job["job_fee"] = job_fee
                if not skip_fee:
                    total_job_fee += job_fee

                # Doba jobu
                if bp_id:
                    bp_time_row = conn.execute(
                        "SELECT manufacturing_time, reaction_time FROM sde_blueprints"
                        " WHERE blueprint_type_id=?", (bp_id,)
                    ).fetchone()
                    base_time = (bp_time_row[1] if is_rxn else bp_time_row[0]) if bp_time_row else None
                    if base_time:
                        activity_name = job.get("activity", "manufacturing")
                        sci_mult, sci_details = _science_skill_mult(conn, bp_id, activity_name, char_skills)
                        job_te = te if not is_rxn else 0
                        # Per-product TE multiplier — Equipment TE rig nezrychluje stavbu lodi
                        prod_facility = rxn_facility if is_rxn else mfg_facility
                        prod_te_mult = get_product_te_multiplier(conn, prod_facility, job["type_id"])
                        job_secs = calc_job_time(
                            base_time=base_time,
                            runs=runs,
                            te=job_te,
                            industry_level=industry_level,
                            adv_industry_level=adv_industry_level,
                            facility_te_multiplier=prod_te_mult,
                            is_reaction=is_rxn,
                            science_skill_mult=sci_mult,
                        )
                        job["facility_te_mult"] = prod_te_mult
                        job["job_duration_seconds"] = job_secs
                        job["job_duration"] = format_duration(job_secs)
                        job["science_skills"] = sci_details  # [(name, level, bonus_pct)]
                        if is_rxn:
                            step_rxn_time = max(step_rxn_time, job_secs)
                        else:
                            step_mfg_time = max(step_mfg_time, job_secs)

            total_mfg_time_s += step_mfg_time
            total_rxn_time_s += step_rxn_time

        # Sbírám unikátní science skilly ze všech jobů pro zobrazení v headeru.
        # Pro stejný skill napříč joby si bereme max required_level.
        _seen: dict[str, tuple[int, float, int]] = {}
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                for sname, slevel, spct, sreq in job.get("science_skills", []):
                    prev = _seen.get(sname)
                    if prev is None:
                        _seen[sname] = (slevel, spct, sreq)
                    else:
                        _seen[sname] = (slevel, spct, max(prev[2], sreq))
        plan_data["all_science_skills"] = [
            (n, l, p, r) for n, (l, p, r) in sorted(_seen.items())
        ]

        # Required Industry / Adv Industry levels — max across all BPs v plánu
        bp_ids_in_plan: set[int] = set()
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                bp_id_j = job.get("blueprint_type_id")
                if bp_id_j:
                    bp_ids_in_plan.add(int(bp_id_j))
        industry_required = 0
        adv_industry_required = 0
        if bp_ids_in_plan:
            ph = ",".join("?" * len(bp_ids_in_plan))
            req_rows = conn.execute(
                f"SELECT skill_type_id, MAX(required_level) FROM sde_blueprint_skills"
                f" WHERE blueprint_type_id IN ({ph}) AND skill_type_id IN (3380, 3388)"
                f" GROUP BY skill_type_id",
                tuple(bp_ids_in_plan),
            ).fetchall()
            for sid, lvl in req_rows:
                if sid == 3380:
                    industry_required = int(lvl)
                elif sid == 3388:
                    adv_industry_required = int(lvl)
        plan_data["industry_required"] = industry_required
        plan_data["adv_industry_required"] = adv_industry_required

        # Tržní cena všech surovin (bez ohledu na sklad)
        full_mat_cost = sum(
            m.get("total_price") or 0.0 for m in plan_data.get("materials", [])
        )
        # Cena jen chybějících surovin (co je potřeba dokoupit)
        buy_cost = plan_data.get("total_buy") or 0.0
        rev = plan_data.get("revenue")

        # Tržní zisk: revenue − všechny suroviny za tržní cenu − job fee
        profit_market = (rev - full_mat_cost - total_job_fee) if rev is not None else None
        # Zisk se zásobami: revenue − jen chybějící suroviny − job fee
        profit_stock  = (rev - buy_cost - total_job_fee) if rev is not None else None

        total_time_s = total_mfg_time_s + total_rxn_time_s
        plan_data["fees"] = {
            "solar_system_id":     solar_system_id,
            "rxn_solar_system_id": rxn_solar_system_id,
            "sep_rxn_station":     sep_rxn_station,
            "mfg_sci":             mfg_sci,
            "rxn_sci":             rxn_sci,
            "facility_tax":        fac_tax_pct,
            "rxn_facility_tax":    rxn_fac_tax_pct,
            "mfg_cost_bonus_pct":  round(mfg_cost_bonus * 100, 1),
            "rxn_cost_bonus_pct":  round(rxn_cost_bonus * 100, 1) if sep_rxn_station else None,
            "total_job_fee":       total_job_fee,
            "total_time_s":        total_time_s,
            "total_time":          format_duration(total_time_s) if total_time_s else None,
            "mfg_te_pct":          round((1 - mfg_te_mult) * 100, 1),
            "rxn_te_pct":          round((1 - rxn_te_mult) * 100, 1) if sep_rxn_station else None,
            "mfg_me_pct":          round((1 - mfg_me_mult) * 100, 2),
            "rxn_me_pct":          round((1 - rxn_me_mult) * 100, 2) if sep_rxn_station else None,
            "full_mat_cost":       full_mat_cost,
            "profit_market":       profit_market,
            "profit_stock":        profit_stock,
        }

    except Exception as e:
        error = str(e)

    # Use the plan character (not the active cookie) for the location dropdown
    location_ids = []
    if plan_char_id_int:
        raw = _load_assets_from_cache(conn, plan_char_id_int)
        location_ids = sorted({a["location_id"] for a in raw if not a.get("is_singleton", False)})

    # Načti jméno stanice pro zobrazení ve formuláři
    loc_names = load_location_names_from_db(conn)
    station_name = loc_names.get(station, str(station))
    rxn_station_name = loc_names.get(reaction_station, str(reaction_station)) if reaction_station else ""

    # Best sell cena produktu na prodejní stanici (z station_volume_cache)
    sell_loc = selling_station if selling_station else station
    station_sell_price: float | None = None
    if plan_data and plan_data.get("product_type_id"):
        svols = get_cached_station_volumes(conn, sell_loc)
        if svols:
            entry = svols.get(plan_data["product_type_id"])
            if entry and entry[1]:
                station_sell_price = entry[1]
    selling_station_name = loc_names.get(sell_loc, str(sell_loc)) if sell_loc else ""

    conn.close()

    return _tr("plan.html", request, {
        "locations": location_ids,
        "result": plan_data,
        "error": error,
        "form_product": product,
        "form_station": station,
        "form_station_name": station_name,
        "form_rxn_station": reaction_station or "",
        "form_rxn_station_name": rxn_station_name,
        "form_qty": qty,
        "form_mode": mode,
        # Po výpočtu vždy zobrazit ROOT BP ME/TE (skutečné hodnoty použité v plánu) —
        # uživatel uvidí konkrétní číslo místo placeholderu.
        "form_me": str(int(me)),
        "form_te": str(int(te)),
        "form_facility_tax": facility_tax,
        "form_rxn_facility_tax": reaction_facility_tax if reaction_facility_tax.strip() else facility_tax,
        "form_facility_me_bonus": facility_me_bonus,
        "form_rxn_me_bonus": reaction_me_bonus,
        "station_sell_price": station_sell_price,
        "station_name": station_name,
        "selling_station_name": selling_station_name,
        "form_selling_station": selling_station or "",
        "form_selling_station_name": selling_station_name if selling_station else "",
        "form_industry":     form_industry,
        "form_adv_industry": form_adv_industry,
        "plan_char_id":      plan_char_id_int,
    })


def _build_manufacturing_steps(root, prices: dict, available: dict) -> list[dict]:
    """
    Výrobní kroky: level 1 = první vyrábět (vše z RAW), level N = poslední.
    Deduplikuje stejný type_id napříč větvemi, agreguje množství.
    """
    from collections import defaultdict

    level_memo: dict[int, int] = {}

    def manufacture_level(node) -> int:
        if node.is_leaf:
            return 0
        if node.type_id in level_memo:
            return level_memo[node.type_id]
        child_levels = [manufacture_level(c) for c in node.children]
        non_zero = [l for l in child_levels if l > 0]
        result = 1 + max(non_zero) if non_zero else 1
        level_memo[node.type_id] = result
        return result

    aggregated: dict[int, dict] = {}
    inputs_agg: dict[int, dict[int, dict]] = {}

    def collect(node):
        if node.is_leaf:
            return
        for child in node.children:
            collect(child)

        tid   = node.type_id
        level = manufacture_level(node)
        sell_p = prices.get(tid, (None, None))[0]

        if tid not in aggregated:
            aggregated[tid] = {
                "type_id":           tid,
                "name":              node.name,
                "quantity":          node.quantity,
                "runs":              node.runs,
                "blueprint_type_id": node.blueprint_type_id,
                "level":             level,
                "activity":          node.activity,
                "me":                node.me,
                "unit_price":        sell_p,
                "total_price":       sell_p * node.quantity if sell_p else None,
                "available":         available.get(tid, 0),
            }
            inputs_agg[tid] = {}
        else:
            aggregated[tid]["quantity"] += node.quantity
            aggregated[tid]["runs"]     += node.runs
            if sell_p:
                aggregated[tid]["total_price"] = sell_p * aggregated[tid]["quantity"]

        for c in node.children:
            c_sell = prices.get(c.type_id, (None, None))[0]
            if c.type_id not in inputs_agg[tid]:
                inputs_agg[tid][c.type_id] = {
                    "type_id":    c.type_id,
                    "name":       c.name,
                    "quantity":   c.quantity,
                    "is_leaf":    c.is_leaf,
                    "activity":   c.activity,
                    "unit_price": c_sell,
                    "total_price": c_sell * c.quantity if c_sell else None,
                    "available":  available.get(c.type_id, 0),
                }
            else:
                inputs_agg[tid][c.type_id]["quantity"] += c.quantity
                if c_sell:
                    inputs_agg[tid][c.type_id]["total_price"] = (
                        c_sell * inputs_agg[tid][c.type_id]["quantity"]
                    )

    collect(root)

    for tid, job in aggregated.items():
        job["inputs"] = sorted(inputs_agg[tid].values(), key=lambda x: x["name"])
        job["input_cost"] = sum(i["total_price"] for i in job["inputs"] if i["total_price"]) or None

    by_level: defaultdict[int, list] = defaultdict(list)
    for job in aggregated.values():
        by_level[job["level"]].append(job)

    max_level = max(by_level.keys()) if by_level else 1
    steps = []
    for level in sorted(by_level.keys()):
        jobs = sorted(by_level[level], key=lambda x: x["name"])
        steps.append({
            "step":       level,
            "jobs":       jobs,
            "total_cost": sum(j["total_price"] for j in jobs if j["total_price"]) or None,
            "is_final":   level == max_level,
        })
    return steps


def _plan_to_dict(plan, prices, type_name: str) -> dict:
    bp = plan.blueprint
    bp_info = None
    if bp:
        bp_info = {
            "kind": "BPO" if bp.is_original else "BPC",
            "me": plan.me,
            "te": plan.te,
            "runs": "∞" if bp.runs == -1 else bp.runs,
        }

    materials = []
    for m in sorted(plan.materials, key=lambda x: (x.ok, x.coverage_pct)):
        sell_p, _ = prices.get(m.type_id, (None, None))
        materials.append({
            "type_id": m.type_id,
            "name": m.name,
            "required": m.required,
            "available": m.available,
            "missing": m.missing,
            "ok": m.ok,
            "coverage_pct": m.coverage_pct,
            "unit_price": sell_p,
            "total_price": sell_p * m.required if sell_p else None,
            "buy_price": sell_p * m.missing if (sell_p and m.missing > 0) else None,
        })

    total_buy = sum(m["buy_price"] for m in materials if m["buy_price"])
    sell_p, _ = prices.get(plan.product_type_id, (None, None))
    revenue = sell_p * plan.quantity if sell_p else None
    profit = (revenue - total_buy) if (revenue and total_buy) else None

    return {
        "product_name": type_name,
        "product_type_id": plan.product_type_id,
        "quantity": plan.quantity,
        "mode": plan.mode,
        "blueprint": bp_info,
        "location_id": plan.location_id,
        "can_manufacture": plan.can_manufacture,
        "total_missing_types": plan.total_missing_types,
        "materials": materials,
        "opt_total_cost": plan.opt_total_cost,
        "opt_naive_cost": plan.opt_naive_cost,
        "total_buy": total_buy,
        "sell_price": sell_p,
        "revenue": revenue,
        "profit": profit,
    }


# ---------------------------------------------------------------------------
# Assety
# ---------------------------------------------------------------------------

@app.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request, search: str = "", view: str = ""):
    conn = get_conn()
    all_chars = list_characters(conn)
    stations: list[dict] = []
    corp_stations: list[dict] = []

    # Resolve which characters to load:
    #   view=all       → every char
    #   view=<id>      → that char
    #   view empty     → active char (cookie / first char)
    selected_chars: list[tuple[int, str]] = []
    if view == "all":
        selected_chars = list(all_chars)
    elif view.isdigit():
        cid = int(view)
        match = next((c for c in all_chars if c[0] == cid), None)
        if match:
            selected_chars = [match]
    if not selected_chars:
        active = get_active_character(request, conn)
        if active:
            selected_chars = [active]

    show_char_badge = view == "all" and len(all_chars) > 1

    # Per-char fetch (uses cache; ESI refresh only when stale)
    char_assets: dict[int, list] = {}            # char_id → personal assets list
    corp_data: dict[int, tuple[int, list]] = {}  # char_id → (corp_id, assets list)
    primary_token: str | None = None
    if selected_chars:
        async with httpx.AsyncClient() as client:
            for cid, _name in selected_chars:
                tok = _get_valid_token_for(conn, cid)
                if not tok:
                    continue
                primary_token = primary_token or tok
                try:
                    char_assets[cid] = await fetch_assets(client, cid, tok, conn)
                except Exception:
                    char_assets[cid] = []
                try:
                    corp_id, corp_list = await fetch_corp_assets(client, cid, tok, conn)
                    if corp_id:
                        update_corporation_id(conn, cid, corp_id)
                    corp_data[cid] = (corp_id, corp_list)
                except Exception:
                    corp_data[cid] = (0, [])

            all_type_ids_for_names = set()
            for assets in char_assets.values():
                all_type_ids_for_names |= {a.type_id for a in assets}
            for _, corp_list in corp_data.values():
                all_type_ids_for_names |= {a.type_id for a in corp_list}
            names = await resolve_names_bulk(conn, list(all_type_ids_for_names), client)
    else:
        names = {}

    if selected_chars:
        char_name_by_id = {cid: name for cid, name in all_chars}

        # ── Personal assets across all selected characters ────────────────
        station_data: dict[int, dict] = {}

        def _get_st(sid: int) -> dict:
            if sid not in station_data:
                station_data[sid] = {"hangar": {}, "containers": {}}
            return station_data[sid]

        # Build a per-char parent_map so container hierarchy resolves correctly
        for owner_id, assets_list in char_assets.items():
            parent_map = {a.item_id: a.location_id for a in assets_list}
            asset_item_ids = {a.item_id for a in assets_list}

            def _hierarchy(a, _items=asset_item_ids, _parents=parent_map) -> tuple[int, int | None]:
                loc = a.location_id
                if loc not in _items:
                    return loc, None
                container_id = loc
                cur = loc
                seen: set[int] = set()
                while cur in _items and cur not in seen:
                    seen.add(cur)
                    cur = _parents.get(cur, cur)
                    if cur not in _items:
                        break
                return cur, container_id

            owner_name = char_name_by_id.get(owner_id, "")
            for a in assets_list:
                item_name = names.get(a.type_id, f"Unknown ({a.type_id})")
                if search and search.lower() not in item_name.lower():
                    continue
                sid, cid = _hierarchy(a)
                st = _get_st(sid)
                bucket = st["hangar"] if cid is None else st["containers"].setdefault(cid, {})
                # Key by (type_id, owner) so different chars stay separate
                key = (a.type_id, owner_id)
                if key in bucket:
                    bucket[key]["quantity"] += a.quantity
                else:
                    bucket[key] = {
                        "type_id": a.type_id,
                        "name": item_name,
                        "quantity": a.quantity,
                        "is_blueprint_copy": a.is_blueprint_copy,
                        "character_id": owner_id,
                        "character_name": owner_name,
                    }

        # Pick a primary char_id for legacy container-name lookups
        char_id = selected_chars[0][0]
        token = primary_token
        # corp_id / corp_assets_list — for single-char view, mirror legacy path;
        # for "all" mode, aggregate distinct corps
        if len(selected_chars) == 1:
            corp_id, corp_assets_list = corp_data.get(char_id, (0, []))
        else:
            corp_id = 0
            corp_assets_list = []
            seen_corp_ids: set[int] = set()
            for cid_corp, c_list in corp_data.values():
                if cid_corp and cid_corp not in seen_corp_ids:
                    seen_corp_ids.add(cid_corp)
                    corp_assets_list = corp_assets_list + c_list
            corp_id = next(iter(seen_corp_ids), 0)

        # ── Corporate assets ─────────────────────────────────────────────────
        # station_id → {div_flag → {"hangar": {type_id: item}, "containers": {cid: {type_id: item}}}}
        corp_sd: dict[int, dict] = {}
        if corp_assets_list:
            corp_item_ids = {a.item_id for a in corp_assets_list}
            corp_parent_map = {a.item_id: a.location_id for a in corp_assets_list}
            corp_flag_map = {a.item_id: a.location_flag for a in corp_assets_list}

            def _corp_hierarchy(a) -> tuple[int, str, int | None]:
                """Returns (station_id, division_flag, container_id|None).

                At NPC stations, items sit inside an office item (flag=OfficeFolder)
                and carry their own CorpSAG* flag. At citadels, items sit directly at
                the structure. In both cases we want the CorpSAG* flag as div_flag.
                """
                loc = a.location_id
                if loc not in corp_item_ids:
                    # Item directly at a station/citadel — its own flag IS the division
                    return loc, a.location_flag, None

                # Walk up the ownership chain to find the station
                chain: list[int] = []
                cur = loc
                seen: set[int] = set()
                while cur in corp_item_ids:
                    if cur in seen:
                        break
                    seen.add(cur)
                    chain.append(cur)
                    nxt = corp_parent_map.get(cur)
                    if nxt is None:
                        break
                    cur = nxt
                station_id = cur

                # Determine the division flag.
                # If the item itself carries a CorpSAG* flag it is directly in a
                # division (NPC office case) — use that flag and no container.
                if a.location_flag in _CORP_DIV_LABEL:
                    return station_id, a.location_flag, None

                # Item is inside a container — scan ancestors for a CorpSAG* flag
                div_flag = "Hangar"
                for ancestor_id in chain:
                    f = corp_flag_map.get(ancestor_id, "")
                    if f in _CORP_DIV_LABEL:
                        div_flag = f
                        break
                return station_id, div_flag, loc

            def _get_corp_div(sid: int, flag: str) -> dict:
                if sid not in corp_sd:
                    corp_sd[sid] = {}
                if flag not in corp_sd[sid]:
                    corp_sd[sid][flag] = {"hangar": {}, "containers": {}}
                return corp_sd[sid][flag]

            for a in corp_assets_list:
                if a.location_flag == "OfficeFolder":
                    continue  # office container itself — structural, not inventory
                item_name = names.get(a.type_id, f"Unknown ({a.type_id})")
                if search and search.lower() not in item_name.lower():
                    continue
                sid, div_flag, cid = _corp_hierarchy(a)
                div = _get_corp_div(sid, div_flag)
                bucket = div["hangar"] if cid is None else div["containers"].setdefault(cid, {})
                if a.type_id in bucket:
                    bucket[a.type_id]["quantity"] += a.quantity
                else:
                    bucket[a.type_id] = {
                        "type_id": a.type_id,
                        "name": item_name,
                        "quantity": a.quantity,
                        "is_blueprint_copy": a.is_blueprint_copy,
                    }

        # ── Prices (shared for both personal and corp) ───────────────────────
        all_price_ids: set[int] = set()
        for sd in station_data.values():
            for bucket in [sd["hangar"], *sd["containers"].values()]:
                all_price_ids |= {item["type_id"] for item in bucket.values()}
        for sid_data in corp_sd.values():
            for dv in sid_data.values():
                for bucket in [dv["hangar"], *dv["containers"].values()]:
                    all_price_ids |= {item["type_id"] for item in bucket.values()}
        all_price_ids = list(all_price_ids)
        prices = await get_prices_for_ids(conn, all_price_ids)

        def _add_prices(bucket: dict):
            for item in bucket.values():
                if item.get("is_blueprint_copy"):
                    item["unit_price"] = None
                    item["total_value"] = None
                else:
                    sell_p, _ = prices.get(item["type_id"], (None, None))
                    item["unit_price"] = sell_p
                    item["total_value"] = sell_p * item["quantity"] if sell_p else None

        for sd in station_data.values():
            _add_prices(sd["hangar"])
            for c in sd["containers"].values():
                _add_prices(c)

        for sid_data in corp_sd.values():
            for dv in sid_data.values():
                _add_prices(dv["hangar"])
                for c in dv["containers"].values():
                    _add_prices(c)

        # ── Location names ────────────────────────────────────────────────────
        all_loc_ids = list(set(station_data.keys()) | set(corp_sd.keys()))
        loc_names = await resolve_station_names_bulk(all_loc_ids, token, conn)

        sys_rows = conn.execute(
            "SELECT location_id, solar_system_id FROM location_name_cache WHERE solar_system_id IS NOT NULL"
        ).fetchall()
        sys_map = {r[0]: r[1] for r in sys_rows}

        # ── Build personal stations ──────────────────────────────────────────
        all_container_ids = [cid for sd in station_data.values() for cid in sd["containers"]]

        # Aggregate assets_raw across all selected chars so container name
        # resolution works for every owner.
        assets_raw_by_char: dict[int, list] = {
            cid: _load_assets_from_cache(conn, cid) for cid, _ in selected_chars
        }
        container_info: dict[int, tuple[str, int]] = {}
        if all_container_ids:
            for owner_id, _ in selected_chars:
                tok = _get_valid_token_for(conn, owner_id)
                if not tok:
                    continue
                owner_assets = assets_raw_by_char.get(owner_id, [])
                owner_info = await _resolve_container_names(
                    owner_id, tok, all_container_ids, owner_assets,
                )
                # First non-empty wins for each container
                for k, v in owner_info.items():
                    container_info.setdefault(k, v)
        container_type_map: dict[int, int] = {}
        # container_id → (owner_character_id, owner_character_name)
        char_name_lookup = {cid: name for cid, name in selected_chars}
        container_owner_map: dict[int, tuple[int, str]] = {}
        for owner_id, ar in assets_raw_by_char.items():
            for item in ar:
                container_type_map[item["item_id"]] = item["type_id"]
                container_owner_map.setdefault(
                    item["item_id"], (owner_id, char_name_lookup.get(owner_id, ""))
                )

        def _sort_items(bucket: dict) -> list:
            return sorted(bucket.values(), key=lambda x: x["name"])

        for sid, sd in station_data.items():
            containers = []
            for cid, items in sd["containers"].items():
                cname = container_info.get(cid, (f"Container {cid}", sid))[0]
                owner = container_owner_map.get(cid)
                containers.append({
                    "container_id": cid,
                    "name": cname,
                    "type_id": container_type_map.get(cid),
                    "assets": _sort_items(items),
                    "character_id":   owner[0] if owner else None,
                    "character_name": owner[1] if owner else "",
                })
            containers.sort(key=lambda c: c["name"])
            hangar_items = _sort_items(sd["hangar"])
            total_items = len(hangar_items) + sum(len(c["assets"]) for c in containers)
            total_value = (
                sum(i.get("total_value") or 0 for i in hangar_items)
                + sum(i.get("total_value") or 0 for c in containers for i in c["assets"])
            )
            stations.append({
                "loc_id": sid,
                "name": loc_names.get(sid, str(sid)),
                "hangar": hangar_items,
                "containers": containers,
                "total_items": total_items,
                "total_value": total_value,
                "solar_system_id": sys_map.get(sid),
            })

        stations.sort(key=lambda s: -s["total_items"])

        # ── Build corp stations ───────────────────────────────────────────────
        if corp_sd and corp_id:
            corp_assets_raw = _load_corp_assets_from_cache(conn, corp_id)
            corp_container_type_map = {item["item_id"]: item["type_id"] for item in corp_assets_raw}
            all_corp_container_ids = [
                cid
                for sid_data in corp_sd.values()
                for dv in sid_data.values()
                for cid in dv["containers"]
            ]
            corp_container_info = await _resolve_corp_container_names(
                corp_id, token, all_corp_container_ids, corp_assets_raw
            ) if all_corp_container_ids else {}

            for sid, sid_data in corp_sd.items():
                divisions = []
                for flag in _CORP_DIV_ORDER:
                    if flag not in sid_data:
                        continue
                    dv = sid_data[flag]
                    containers = []
                    for cid, items in dv["containers"].items():
                        cname = corp_container_info.get(cid, (f"Container {cid}", sid))[0]
                        containers.append({
                            "container_id": cid,
                            "name": cname,
                            "type_id": corp_container_type_map.get(cid),
                            "assets": _sort_items(items),
                        })
                    containers.sort(key=lambda c: c["name"])
                    hangar_items = _sort_items(dv["hangar"])
                    if not hangar_items and not containers:
                        continue
                    divisions.append({
                        "flag": flag,
                        "label": _CORP_DIV_LABEL.get(flag, flag),
                        "hangar": hangar_items,
                        "containers": containers,
                    })
                # Also include any flags not in _CORP_DIV_ORDER
                for flag, dv in sid_data.items():
                    if flag in _CORP_DIV_ORDER:
                        continue
                    hangar_items = _sort_items(dv["hangar"])
                    containers = [
                        {
                            "container_id": cid,
                            "name": corp_container_info.get(cid, (f"Container {cid}", sid))[0],
                            "type_id": corp_container_type_map.get(cid),
                            "assets": _sort_items(items),
                        }
                        for cid, items in dv["containers"].items()
                    ]
                    if hangar_items or containers:
                        divisions.append({
                            "flag": flag,
                            "label": flag,
                            "hangar": hangar_items,
                            "containers": sorted(containers, key=lambda c: c["name"]),
                        })

                total_items = sum(
                    len(d["hangar"]) + sum(len(c["assets"]) for c in d["containers"])
                    for d in divisions
                )
                total_value = sum(
                    sum(i.get("total_value") or 0 for i in d["hangar"])
                    + sum(i.get("total_value") or 0 for c in d["containers"] for i in c["assets"])
                    for d in divisions
                )
                corp_stations.append({
                    "loc_id": sid,
                    "name": loc_names.get(sid, str(sid)),
                    "divisions": divisions,
                    "total_items": total_items,
                    "total_value": total_value,
                    "solar_system_id": sys_map.get(sid),
                })

            corp_stations.sort(key=lambda s: -s["total_items"])

    conn.close()
    return _tr("assets.html", request, {
        "stations": stations,
        "corp_stations": corp_stations,
        "search": search,
        "view": view or "",
        "show_char_badge": show_char_badge,
        "selected_chars": selected_chars,
    })


@app.get("/api/assets/distances")
async def assets_distances(request: Request):
    """Vrátí počet jumpů z aktuální pozice postavy ke každé lokaci v assets."""
    conn = get_conn()
    char = get_active_character(request, conn)
    token = get_active_token(request, conn)
    if not char or not token:
        conn.close()
        return {"ok": False, "error": "Nepřihlášen"}
    char_id, _ = char

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://esi.evetech.net/latest/characters/{char_id}/location/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    if r.status_code != 200:
        conn.close()
        return {"ok": False, "error": "Nepodařilo se zjistit lokaci postavy"}
    origin_sys = r.json().get("solar_system_id")
    if not origin_sys:
        conn.close()
        return {"ok": False, "error": "Postava není v solárním systému"}

    rows = conn.execute(
        "SELECT location_id, solar_system_id FROM location_name_cache WHERE solar_system_id IS NOT NULL"
    ).fetchall()
    conn.close()
    loc_to_sys = {row[0]: row[1] for row in rows}

    # Deduplikuj systémy — jeden ESI call na unikátní destinaci
    unique_sys = list(set(loc_to_sys.values()))

    async def _jumps(client: httpx.AsyncClient, dest: int) -> int:
        if dest == origin_sys:
            return 0
        try:
            resp = await client.get(
                f"https://esi.evetech.net/latest/route/{origin_sys}/{dest}/",
                params={"datasource": "tranquility"},
                timeout=10,
            )
            return len(resp.json()) - 1 if resp.status_code == 200 else -1
        except Exception:
            return -1

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_jumps(client, s) for s in unique_sys])
    sys_jumps = dict(zip(unique_sys, results))

    distances = {loc_id: sys_jumps.get(sys_id, -1) for loc_id, sys_id in loc_to_sys.items()}
    return {"ok": True, "origin_sys": origin_sys, "distances": distances}


# ---------------------------------------------------------------------------
# Blueprinty
# ---------------------------------------------------------------------------

async def _resolve_corp_container_names(
    corp_id: int,
    token: str,
    container_ids: list[int],
    corp_assets_raw: list[dict],
) -> dict[int, tuple[str, int]]:
    """Corp variant of _resolve_container_names using corp ESI endpoint."""
    asset_map = {item["item_id"]: item for item in corp_assets_raw}
    result: dict[int, tuple[str, int]] = {}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://esi.evetech.net/latest/corporations/{corp_id}/assets/names/",
                params={"datasource": "tranquility"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=json.dumps(container_ids),
                timeout=10,
            )
            custom_names = {e["item_id"]: e["name"] for e in r.json()} if r.status_code == 200 else {}
    except Exception:
        custom_names = {}

    type_id_set = {asset_map[cid]["type_id"] for cid in container_ids if cid in asset_map}
    type_names: dict[int, str] = {}
    if type_id_set:
        conn_local = get_conn()
        ph = ",".join("?" * len(type_id_set))
        rows = conn_local.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_id_set)
        ).fetchall()
        conn_local.close()
        type_names = {r[0]: r[1] for r in rows}

    for cid in container_ids:
        asset = asset_map.get(cid)
        if not asset:
            continue
        parent_loc = asset["location_id"]
        raw_name = custom_names.get(cid, "")
        display = raw_name if raw_name else type_names.get(asset["type_id"], f"Container {cid}")
        result[cid] = (display, parent_loc)

    return result


async def _resolve_container_names(
    char_id: int,
    token: str,
    container_ids: list[int],
    assets: list[dict],
) -> dict[int, tuple[str, int]]:
    """Pro container item_ids vrátí {container_id: (display_name, parent_location_id)}.

    display_name je custom jméno kontejneru z ESI assets/names,
    nebo typ kontejneru (Small Secure Container apod.) jako fallback.
    parent_location_id je location_id kontejneru v assets (stanice/struktura).
    """
    asset_map = {item["item_id"]: item for item in assets}
    result: dict[int, tuple[str, int]] = {}

    parent_ids = {asset_map[cid]["location_id"] for cid in container_ids if cid in asset_map}
    _ = parent_ids  # parent IDs se resolvují zvlášť přes resolve_station_names_bulk

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://esi.evetech.net/latest/characters/{char_id}/assets/names/",
                params={"datasource": "tranquility"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=json.dumps(container_ids),
                timeout=10,
            )
            custom_names = {e["item_id"]: e["name"] for e in r.json()} if r.status_code == 200 else {}
    except Exception:
        custom_names = {}

    type_id_set = {asset_map[cid]["type_id"] for cid in container_ids if cid in asset_map}
    type_names: dict[int, str] = {}
    if type_id_set:
        conn_local = get_conn()
        ph = ",".join("?" * len(type_id_set))
        rows = conn_local.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_id_set)
        ).fetchall()
        conn_local.close()
        type_names = {r[0]: r[1] for r in rows}

    for cid in container_ids:
        asset = asset_map.get(cid)
        if not asset:
            continue
        parent_loc = asset["location_id"]
        raw_name = custom_names.get(cid, "")
        if raw_name:
            display = raw_name
        else:
            display = type_names.get(asset["type_id"], f"Kontejner {cid}")
        result[cid] = (display, parent_loc)

    return result


@app.get("/blueprints", response_class=HTMLResponse)
async def blueprints_page(request: Request, search: str = "", view: str = ""):
    conn = get_conn()
    all_chars = list_characters(conn)

    # Resolve selected character(s) — same toggle pattern as /assets
    selected_chars: list[tuple[int, str]] = []
    if view == "all":
        selected_chars = list(all_chars)
    elif view.isdigit():
        cid = int(view)
        match = next((c for c in all_chars if c[0] == cid), None)
        if match:
            selected_chars = [match]
    if not selected_chars:
        active = get_active_character(request, conn)
        if active:
            selected_chars = [active]
    show_char_badge = view == "all" and len(all_chars) > 1

    bp_list: list[dict] = []
    bps_by_char: dict[int, list] = {}
    primary_token: str | None = None
    char_name_by_id = {cid: name for cid, name in all_chars}

    if selected_chars:
        async with httpx.AsyncClient() as client:
            all_unique_type_ids: set[int] = set()
            for cid_sel, _name in selected_chars:
                tok = _get_valid_token_for(conn, cid_sel)
                if not tok:
                    continue
                primary_token = primary_token or tok
                try:
                    bps_for = await fetch_blueprints(client, cid_sel, tok, conn)
                except Exception:
                    bps_for = []
                bps_by_char[cid_sel] = bps_for
                all_unique_type_ids |= {bp.type_id for bp in bps_for}
            names = await resolve_names_bulk(conn, list(all_unique_type_ids), client)

        if all_unique_type_ids:
            ph = ",".join("?" * len(all_unique_type_ids))
            prod_rows = conn.execute(
                f"SELECT blueprint_type_id, product_type_id FROM sde_blueprint_products"
                f" WHERE blueprint_type_id IN ({ph}) AND activity IN ('manufacturing','reaction')",
                list(all_unique_type_ids),
            ).fetchall()
            product_type_map = {r[0]: r[1] for r in prod_rows}
        else:
            product_type_map = {}

        for owner_id, bps in bps_by_char.items():
            owner_name = char_name_by_id.get(owner_id, "")
            for bp in bps:
                name = names.get(bp.type_id, f"Unknown ({bp.type_id})")
                if search and search.lower() not in name.lower():
                    continue
                bp_list.append({
                    "name": name,
                    "type_id": bp.type_id,
                    "product_type_id": product_type_map.get(bp.type_id, bp.type_id),
                    "is_original": bp.is_original,
                    "me": bp.material_efficiency,
                    "te": bp.time_efficiency,
                    "runs": "∞" if bp.runs == -1 else bp.runs,
                    "location_id": bp.location_id,
                    "character_id": owner_id,
                    "character_name": owner_name,
                })
        bp_list.sort(key=lambda x: x["name"])

    token = primary_token
    char = selected_chars[0] if selected_chars else None
    char_id = char[0] if char else 0

    from collections import defaultdict

    # Aggregate assets across selected chars for container detection
    assets: list[dict] = []
    assets_by_char: dict[int, list[dict]] = {}
    for cid_sel, _ in selected_chars:
        a = _load_assets_from_cache(conn, cid_sel)
        assets_by_char[cid_sel] = a
        assets.extend(a)
    asset_item_ids = {item["item_id"] for item in assets}

    all_raw_loc_ids = list({bp["location_id"] for bp in bp_list})
    container_ids = [lid for lid in all_raw_loc_ids if lid in asset_item_ids]
    structure_ids = [lid for lid in all_raw_loc_ids if lid not in asset_item_ids]

    # Resolvuj jména stanic
    loc_names = await resolve_station_names_bulk(structure_ids, token, conn) if structure_ids else {}

    # Resolvuj jména kontejnerů + jejich parent stanice (per char)
    container_info: dict[int, tuple[str, int]] = {}
    if container_ids:
        for owner_id, _ in selected_chars:
            tok = _get_valid_token_for(conn, owner_id)
            if not tok:
                continue
            owner_assets = assets_by_char.get(owner_id, [])
            owner_info = await _resolve_container_names(
                owner_id, tok, container_ids, owner_assets,
            )
            for k, v in owner_info.items():
                container_info.setdefault(k, v)
        parent_ids_to_resolve = list({info[1] for info in container_info.values()
                                      if info[1] not in loc_names})
        if parent_ids_to_resolve and token:
            parent_names = await resolve_station_names_bulk(parent_ids_to_resolve, token, conn)
            loc_names.update(parent_names)

    # Sestavení hierarchie: {station_id: {"hangar": [...], "containers": {cid: {"name": ..., "bps": [...]}}}}
    station_data: dict[int, dict] = {}

    def _get_station(sid: int) -> dict:
        if sid not in station_data:
            station_data[sid] = {"hangar": [], "containers": {}}
        return station_data[sid]

    for bp in bp_list:
        lid = bp["location_id"]
        if lid in container_info:
            container_name, parent_loc = container_info[lid]
            st = _get_station(parent_loc)
            if lid not in st["containers"]:
                st["containers"][lid] = {"name": container_name, "bps": []}
            st["containers"][lid]["bps"].append(bp)
        else:
            _get_station(lid)["hangar"].append(bp)

    # Převeď na seznam seřazený podle celkového počtu
    def _station_total(sd: dict) -> int:
        return len(sd["hangar"]) + sum(len(c["bps"]) for c in sd["containers"].values())

    stations = sorted(
        [
            {
                "loc_id": sid,
                "name": loc_names.get(sid, str(sid)),
                "hangar": sd["hangar"],
                "containers": sorted(sd["containers"].values(), key=lambda c: c["name"]),
                "total": _station_total(sd),
            }
            for sid, sd in station_data.items()
        ],
        key=lambda s: -s["total"],
    )

    conn.close()
    return _tr("blueprints.html", request, {
        "stations": stations,
        "search": search,
        "total": len(bp_list),
        "view": view or "",
        "show_char_badge": show_char_badge,
    })


# ---------------------------------------------------------------------------
# Ceny
# ---------------------------------------------------------------------------

@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request):
    conn = get_conn()
    stats = get_price_cache_stats(conn)
    # Default render jen relevantní podmnožinu (user assets + BPs + custom prices).
    # Plná cache má ~19k itemů → render celé tabulky = 48 MB HTML. Zbytek se
    # loaduje přes /api/prices/search na vyžádání.
    # Aggregate user type-IDs across ALL characters so prices page reflects every alt.
    relevant: set[int] = set()
    for char_id, _name in list_characters(conn):
        relevant |= {a["type_id"] for a in _load_assets_from_cache(conn, char_id)}
        relevant |= {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char_id)}
    if relevant:
        ph = ",".join("?" * len(relevant))
        bp_products = conn.execute(
            f"SELECT product_type_id FROM sde_blueprint_products"
            f" WHERE blueprint_type_id IN ({ph})",
            tuple(relevant),
        ).fetchall()
        relevant |= {r[0] for r in bp_products}
    items = get_all_price_items(conn, relevant_ids=relevant)
    conn.close()
    return _tr("prices.html", request, {
        "stats": stats,
        "refreshed_count": None,
        "total_requested": None,
        "items": items,
    })


@app.get("/api/station-industry-info")
async def station_industry_info(request: Request, location_id: int):
    """
    Vrátí SCI, facility tax, ME bonus a security multiplier pro zadanou stanici/strukturu.
    Facility tax se odvozuje z nedávných jobů postavy (cost/EIV − SCI).
    """
    conn = get_conn()
    sys_row = conn.execute(
        "SELECT solar_system_id FROM location_name_cache WHERE location_id=?",
        (location_id,),
    ).fetchone()
    solar_system_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

    mfg_sci = rxn_sci = 0.0
    security_status: float | None = None
    if solar_system_id:
        mfg_sci = await get_sci_for_system(conn, solar_system_id, "manufacturing")
        rxn_sci = await get_sci_for_system(conn, solar_system_id, "reaction")
        # Pre-fetch security_status do cache, aby synchronní helper get_station_me_bonus_pct
        # mohl správně škálovat rig bonusy (×1.0 / ×1.9 / ×2.1).
        security_status = await get_security_status(conn, solar_system_id)

    # Facility tax neumíme načíst přesně z ESI (derive z průměru jobů byl nepřesný).
    # Uživatel zadává ručně, hodnotu si může uložit jako default (localStorage).
    rig_info = get_station_rigs_full(conn, location_id)
    # ME bonus přepočítaný se security multiplierem (přepisuje stale stored value)
    me_bonus_live = get_station_me_bonus_pct(conn, location_id)
    conn.close()
    return {
        "solar_system_id":  solar_system_id,
        "security_status":  security_status,
        "mfg_sci":          mfg_sci,
        "rxn_sci":          rxn_sci,
        "me_bonus_pct":     me_bonus_live,
        "structure_type":   rig_info["structure_type"],
        "rigs":             rig_info["rigs"],
    }


@app.post("/api/station-rigs")
async def save_station_rigs(request: Request):
    """Uloží konfiguraci rigů pro danou stanici/strukturu."""
    try:
        data = await request.json()
        location_id = int(data.get("location_id", 0))
        if not location_id:
            return {"ok": False, "error": "missing location_id"}
        structure_type = data.get("structure_type") or None
        rig1 = int(data["rig1_type_id"]) if data.get("rig1_type_id") else None
        rig2 = int(data["rig2_type_id"]) if data.get("rig2_type_id") else None
        rig3 = int(data["rig3_type_id"]) if data.get("rig3_type_id") else None
        conn = get_conn()
        save_station_rigs_full(conn, location_id, structure_type, rig1, rig2, rig3)
        # Vrátit security-adjusted ME bonus (helper aplikuje sec multiplier na rigy)
        me_bonus = get_station_me_bonus_pct(conn, location_id)
        conn.close()
        return {"ok": True, "me_bonus_pct": me_bonus}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/rig-types")
async def api_rig_types(structure_type: str = ""):
    """Vrátí dostupné rigy pro daný typ struktury (raitaru/azbel/sotiyo/athanor/tatara)."""
    conn = get_conn()
    populate_rig_bonuses(conn)
    rigs = get_rig_types(conn, structure_type)
    conn.close()
    return {"rigs": rigs}


@app.get("/api/suggest-station")
async def suggest_station(request: Request, q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    ensure_location_name_table(conn)
    char = get_active_character(request, conn)
    token = get_active_token(request, conn)
    pattern = q.strip().lower()

    # Lokace kde má postava assety (osobní + korporátní)
    asset_locs: set[int] = set()
    if char:
        raw = _load_assets_from_cache(conn, char[0])
        for a in raw:
            if not a.get("is_singleton", False):
                asset_locs.add(a["location_id"])

    all_names = load_location_names_from_db(conn)
    cache_empty = len(all_names) == 0

    # Stanice s assety — filtruj podle jména
    owned_ids: set[int] = set()
    owned = []
    for loc_id in asset_locs:
        name = all_names.get(loc_id, str(loc_id))
        if pattern in name.lower() or pattern in str(loc_id):
            owned.append({"location_id": loc_id, "name": name})
            owned_ids.add(loc_id)
    owned.sort(key=lambda x: x["name"])

    # Ostatní known stanice z cache bez assetů
    other = []
    other_ids: set[int] = set()
    for loc_id, name in all_names.items():
        if loc_id not in asset_locs and (pattern in name.lower() or pattern in str(loc_id)):
            other.append({"location_id": loc_id, "name": name})
            other_ids.add(loc_id)
    other.sort(key=lambda x: x["name"])

    # ESI search — NPC stanice + systémy + player struktury (paralelně)
    try:
        async with httpx.AsyncClient() as client:
            esi_tasks: list = [
                client.get(
                    "https://esi.evetech.net/latest/search/",
                    params={"categories": "station", "search": q.strip(),
                            "datasource": "tranquility", "strict": "false"},
                    timeout=5.0,
                ),
                client.get(
                    "https://esi.evetech.net/latest/search/",
                    params={"categories": "solar_system", "search": q.strip(),
                            "datasource": "tranquility", "strict": "false"},
                    timeout=5.0,
                ),
            ]
            # Autentizovaný search pro player struktury (citadely, engineering complexes…)
            if char and token:
                esi_tasks.append(
                    client.get(
                        f"https://esi.evetech.net/latest/characters/{char[0]}/search/",
                        params={"categories": "structure", "search": q.strip(),
                                "datasource": "tranquility", "strict": "false"},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=5.0,
                    )
                )

            results = await asyncio.gather(*esi_tasks, return_exceptions=True)
            station_search = results[0]
            system_search = results[1]
            structure_search = results[2] if len(results) > 2 else None

            # NPC stanice — přímý výsledek z ESI search
            if not isinstance(station_search, Exception) and station_search.status_code == 200:
                npc_ids = station_search.json().get("station", [])[:20]
                new_ids = [sid for sid in npc_ids if sid not in all_names]
                if new_ids:
                    new_names = await resolve_station_names_bulk(new_ids, token=None, conn=conn)
                    all_names.update(new_names)
                for sid in npc_ids:
                    if sid in asset_locs and sid not in owned_ids:
                        owned.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        owned_ids.add(sid)
                    elif sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            # Player struktury — výsledek z autentizovaného character search
            if (structure_search and not isinstance(structure_search, Exception)
                    and structure_search.status_code == 200):
                struct_ids = structure_search.json().get("structure", [])[:20]
                new_struct_ids = [sid for sid in struct_ids if sid not in all_names]
                if new_struct_ids:
                    new_names = await resolve_station_names_bulk(new_struct_ids, token=token, conn=conn)
                    all_names.update(new_names)
                for sid in struct_ids:
                    if sid in asset_locs and sid not in owned_ids:
                        owned.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        owned_ids.add(sid)
                    elif sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            # Systémy — najdi struktury v naší cache + NPC stanice v systému
            system_ids: list[int] = []
            if not isinstance(system_search, Exception) and system_search.status_code == 200:
                system_ids = system_search.json().get("solar_system", [])

            for sys_id in system_ids[:10]:
                for entry in locations_in_system(conn, sys_id):
                    lid = entry["location_id"]
                    if lid in asset_locs and lid not in owned_ids:
                        owned.append(entry)
                        owned_ids.add(lid)
                    elif lid not in asset_locs and lid not in other_ids:
                        other.append(entry)
                        other_ids.add(lid)

            # NPC stanice v nalezených systémech
            sys_tasks = [
                client.get(
                    f"https://esi.evetech.net/latest/universe/systems/{sid}/",
                    params={"datasource": "tranquility"}, timeout=4.0,
                )
                for sid in system_ids[:5]
            ]
            if sys_tasks:
                sys_results = await asyncio.gather(*sys_tasks, return_exceptions=True)
                new_npc: list[int] = []
                for sys_r in sys_results:
                    if not isinstance(sys_r, Exception) and sys_r.status_code == 200:
                        new_npc.extend(sys_r.json().get("stations", []))
                new_npc_ids = [sid for sid in new_npc if sid not in all_names]
                if new_npc_ids:
                    new_names = await resolve_station_names_bulk(new_npc_ids, token=None, conn=conn)
                    all_names.update(new_names)
                for sid in new_npc:
                    if sid not in asset_locs and sid not in other_ids:
                        other.append({"location_id": sid, "name": all_names.get(sid, str(sid))})
                        other_ids.add(sid)

            other.sort(key=lambda x: x["name"])
            owned.sort(key=lambda x: x["name"])
    except Exception:
        pass

    conn.close()
    return {"owned": owned[:15], "other": other[:10], "cache_empty": cache_empty and not owned and not other}


@app.post("/api/add-station")
async def add_station(request: Request, raw: str = Form(...)):
    """
    Přidá strukturu do cache. Přijímá:
    - ID struktury (číslo)
    - EVE URL formát: <url=showinfo:TYPE//ID>Jméno</url>
    - ID<mezera>Jméno: např. "1045667241057 C-N4OD - Fortizar"
    """
    import re
    conn = get_conn()
    ensure_location_name_table(conn)
    token = get_active_token(request, conn)

    raw = raw.strip()
    structure_id: int | None = None
    hint_name: str | None = None

    # EVE URL format: showinfo:TYPE//ID nebo showinfo:TYPE//ID>Jméno
    m = re.search(r'showinfo:\d+//(\d+)(?:[^>]*>([^<]+))?', raw)
    if m:
        structure_id = int(m.group(1))
        hint_name = m.group(2).strip() if m.group(2) else None
    # Jen číslo, nebo "ID jméno"
    elif raw:
        parts = raw.split(None, 1)
        if parts[0].isdigit():
            structure_id = int(parts[0])
            hint_name = parts[1].strip() if len(parts) > 1 else None

    if not structure_id:
        return {"error": "Nelze rozpoznat ID struktury"}, 400

    resolved_name = hint_name
    sys_id: int | None = None

    # Zkus ESI
    try:
        async with httpx.AsyncClient() as client:
            if structure_id < 1_000_000_000_000:
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/stations/{structure_id}/",
                    params={"datasource": "tranquility"}, timeout=8,
                )
            else:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/structures/{structure_id}/",
                    params={"datasource": "tranquility"}, headers=headers, timeout=8,
                )
            if r.status_code == 200:
                data = r.json()
                resolved_name = data.get("name") or resolved_name
                sys_id = data.get("solar_system_id") or data.get("system_id")
    except Exception:
        pass

    if not resolved_name:
        resolved_name = f"[Struktura {structure_id}]"

    conn.execute(
        "INSERT OR REPLACE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?,?,?)",
        (structure_id, resolved_name, sys_id),
    )
    conn.commit()
    conn.close()
    return {"location_id": structure_id, "name": resolved_name, "solar_system_id": sys_id}


@app.post("/api/location/rename")
async def location_rename(request: Request):
    """Uloží uživatelem zadaný název lokace do cache."""
    body = await request.json()
    location_id = int(body["location_id"])
    name = str(body.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "Prázdný název"}
    conn = get_conn()
    ensure_location_name_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO location_name_cache (location_id, name) VALUES (?,?)",
        (location_id, name),
    )
    conn.commit()
    conn.close()
    from app.web.location_resolver import _cache
    _cache[location_id] = name
    return {"ok": True, "location_id": location_id, "name": name}


@app.get("/api/location/resolve")
async def location_resolve(request: Request, location_id: int):
    """Pokusí se dohledat jméno struktury přes ESI s aktuálním tokenem."""
    conn = get_conn()
    token = get_active_token(request, conn)
    if not token:
        conn.close()
        return {"ok": False, "error": "Nepřihlášen"}
    from app.web.location_resolver import resolve_station_name, _cache
    _cache.pop(location_id, None)  # vynutí čerstvé ESI volání
    async with httpx.AsyncClient() as client:
        name, sys_id = await resolve_station_name(client, location_id, token)
    resolved = name != str(location_id) and not name.startswith("[Privátní")
    if resolved:
        ensure_location_name_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?,?,?)",
            (location_id, name, sys_id),
        )
        conn.commit()
    conn.close()
    return {"ok": resolved, "name": name, "solar_system_id": sys_id}


@app.get("/api/my-location")
async def my_location(request: Request):
    """Vrátí aktuální lokaci postavy (structure_id pokud je docknutá ve struktuře)."""
    conn = get_conn()
    token = get_active_token(request, conn)
    char = get_active_character(request, conn)
    if not token or not char:
        conn.close()
        return {"error": "Nepřihlášen"}
    ensure_location_name_table(conn)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://esi.evetech.net/latest/characters/{char[0]}/location/",
                params={"datasource": "tranquility"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            if r.status_code != 200:
                return {"error": f"ESI {r.status_code}"}
            loc = r.json()

        structure_id: int | None = loc.get("structure_id") or loc.get("station_id")
        sys_id: int = loc.get("solar_system_id", 0)

        if not structure_id:
            # Načti jméno systému
            async with httpx.AsyncClient() as client:
                sr = await client.get(
                    f"https://esi.evetech.net/latest/universe/systems/{sys_id}/",
                    params={"datasource": "tranquility"}, timeout=5,
                )
                sys_name = sr.json().get("name", str(sys_id)) if sr.status_code == 200 else str(sys_id)
            return {"in_space": True, "solar_system_id": sys_id, "solar_system_name": sys_name}

        # Vyřeš jméno struktury/stanice a ulož do cache
        resolved_name = str(structure_id)
        try:
            async with httpx.AsyncClient() as client:
                if structure_id < 1_000_000_000_000:
                    r2 = await client.get(
                        f"https://esi.evetech.net/latest/universe/stations/{structure_id}/",
                        params={"datasource": "tranquility"}, timeout=8,
                    )
                else:
                    r2 = await client.get(
                        f"https://esi.evetech.net/latest/universe/structures/{structure_id}/",
                        params={"datasource": "tranquility"},
                        headers={"Authorization": f"Bearer {token}"}, timeout=8,
                    )
                if r2.status_code == 200:
                    data = r2.json()
                    resolved_name = data.get("name", resolved_name)
                    sys_id = data.get("solar_system_id") or data.get("system_id") or sys_id
        except Exception:
            pass

        conn.execute(
            "INSERT OR REPLACE INTO location_name_cache (location_id, name, solar_system_id) VALUES (?,?,?)",
            (structure_id, resolved_name, sys_id),
        )
        conn.commit()
        conn.close()
        return {"location_id": structure_id, "name": resolved_name,
                "solar_system_id": sys_id, "in_space": False}
    except Exception as e:
        conn.close()
        return {"error": str(e)}


@app.get("/api/plan/fetch-sell-price")
async def fetch_plan_sell_price(request: Request, location_id: int, type_id: int):
    """Načte best sell cenu konkrétního produktu na zadané stanici, uloží do station_volume_cache."""
    conn = get_conn()
    token = get_active_token(request, conn)
    ensure_price_table(conn)

    # Zajisti přítomnost type_id v market_price_cache (fetchery to potřebují pro filtrování)
    conn.execute(
        "INSERT OR IGNORE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,NULL,NULL,0)",
        (type_id,),
    )
    conn.commit()

    region_id = await get_region_for_location(conn, location_id, token)

    try:
        if location_id >= 1_000_000_000:
            if not token:
                conn.close()
                return {"ok": False, "error": "Pro přístup k marketu struktury je nutné přihlášení."}
            result = await fetch_structure_market(conn, location_id, token, {type_id}, region_id)
        else:
            if not region_id:
                conn.close()
                return {"ok": False, "error": "Nepodařilo se určit region pro tuto lokaci."}
            result = await fetch_station_volumes(conn, location_id, region_id, [type_id])
    except PermissionError as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}

    conn.close()
    best_sell = result.get(type_id, (None, None, None))[1] if result else None
    return {"ok": True, "best_sell": best_sell}


# ── Projects ────────────────────────────────────────────────────────────────

@app.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    conn = get_conn()
    projects = list_projects(conn)
    conn.close()
    return _tr("projects.html", request, {"projects": projects})


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(request: Request, project_id: int):
    conn = get_conn()
    detail = get_project_detail(conn, project_id)
    conn.close()
    if not detail:
        return HTMLResponse("Projekt nenalezen", status_code=404)
    return _tr("project_detail.html", request, {"project": detail})


@app.get("/api/projects/list")
async def api_projects_list():
    conn = get_conn()
    projects = list_projects(conn)
    conn.close()
    return {"projects": projects}


@app.post("/api/projects/new")
async def api_project_new(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "Název nesmí být prázdný"}
    conn = get_conn()
    pid = create_project(conn, name)
    conn.close()
    return {"ok": True, "project_id": pid, "name": name}


@app.post("/api/projects/{project_id}/add-plan")
async def api_project_add_plan(project_id: int, request: Request):
    body = await request.json()
    plan_data = body.get("plan_data")
    if not plan_data:
        return {"ok": False, "error": "Chybí data plánu"}
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM production_projects WHERE id=?", (project_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "Projekt nenalezen"}
    plan_id = add_plan_to_project(
        conn, project_id, plan_data,
        body.get("station_name", ""),
        float(body.get("facility_tax", 0)),
    )
    conn.close()
    return {"ok": True, "plan_id": plan_id}


@app.post("/api/project-jobs/toggle")
async def api_project_job_toggle(request: Request):
    """Toggle status of one or more job IDs (merged jobs share type_id+step)."""
    body = await request.json()
    job_ids = body.get("job_ids", [])
    target = body.get("status")  # "completed" or "pending"
    if not job_ids or target not in ("completed", "pending"):
        return {"ok": False, "error": "bad request"}
    conn = get_conn()
    ph = ",".join("?" * len(job_ids))
    conn.execute(
        f"UPDATE project_jobs SET status=? WHERE id IN ({ph})",
        [target] + list(job_ids),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "status": target}


@app.post("/api/project-shopping/update")
async def api_project_shopping_update(request: Request):
    body = await request.json()
    project_id = int(body.get("project_id", 0))
    type_id = int(body.get("type_id", 0))
    purchased = int(body.get("purchased", 0))
    if not project_id or not type_id:
        return {"ok": False}
    conn = get_conn()
    conn.execute(
        "UPDATE project_shopping SET purchased=? WHERE project_id=? AND type_id=?",
        (purchased, project_id, type_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/projects/{project_id}/shopping/mark-all")
async def api_project_shopping_mark_all(project_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE project_shopping SET purchased=needed WHERE project_id=?", (project_id,)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/project-plans/{plan_id}/toggle")
async def api_project_plan_toggle(plan_id: int, request: Request):
    body = await request.json()
    status = body.get("status", "completed")
    conn = get_conn()
    conn.execute("UPDATE project_plans SET status=? WHERE id=?", (status, plan_id))
    conn.commit()
    conn.close()
    return {"ok": True, "status": status}


@app.delete("/api/projects/{project_id}")
async def api_project_delete(project_id: int):
    conn = get_conn()
    for tbl in ("project_jobs", "project_shopping", "project_plans", "production_projects"):
        col = "id" if tbl == "production_projects" else "project_id"
        conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/suggest")
async def suggest(request: Request, q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    char = get_active_character(request, conn)
    pattern = f"%{q.strip().lower()}%"
    owned: list[dict] = []
    owned_product_ids: set[int] = set()

    if char:
        char_id, _ = char
        raw_bps = _load_blueprints_from_cache(conn, char_id)
        if raw_bps:
            bp_type_ids = list({bp["type_id"] for bp in raw_bps})
            # Seskup podle type_id — vyber nejlepší (BPO > BPC, nejvyšší ME)
            bp_by_type: dict[int, dict] = {}
            for bp in raw_bps:
                tid = bp["type_id"]
                if tid not in bp_by_type:
                    bp_by_type[tid] = bp
                else:
                    cur = bp_by_type[tid]
                    if (bp.get("quantity", 1) == -1, bp.get("material_efficiency", 0)) > \
                       (cur.get("quantity", 1) == -1, cur.get("material_efficiency", 0)):
                        bp_by_type[tid] = bp

            ph = ",".join("?" * len(bp_type_ids))
            rows = conn.execute(f"""
                SELECT sbp.blueprint_type_id, sbp.product_type_id, t.name
                FROM sde_blueprint_products sbp
                JOIN sde_types t ON t.type_id = sbp.product_type_id
                WHERE sbp.blueprint_type_id IN ({ph})
                  AND sbp.activity IN ('manufacturing', 'reaction')
                  AND LOWER(t.name) LIKE ?
                ORDER BY t.name
            """, bp_type_ids + [pattern]).fetchall()

            for bp_type_id, product_type_id, product_name in rows:
                owned_product_ids.add(product_type_id)
                bp = bp_by_type.get(bp_type_id, {})
                is_original = bp.get("quantity", 1) == -1
                runs = bp.get("runs", -1)
                owned.append({
                    "name": product_name,
                    "type_id": product_type_id,
                    "me": bp.get("material_efficiency", 0),
                    "te": bp.get("time_efficiency", 0),
                    "is_original": is_original,
                    "runs": "∞" if runs == -1 else runs,
                })

    # SDE — ostatní blueprinty (nevlastněné)
    if owned_product_ids:
        ph2 = ",".join("?" * len(owned_product_ids))
        other_rows = conn.execute(f"""
            SELECT DISTINCT t.type_id, t.name
            FROM sde_types t
            JOIN sde_blueprint_products sbp ON sbp.product_type_id = t.type_id
            WHERE LOWER(t.name) LIKE ?
              AND sbp.activity IN ('manufacturing', 'reaction')
              AND t.type_id NOT IN ({ph2})
            ORDER BY t.name LIMIT 15
        """, [pattern] + list(owned_product_ids)).fetchall()
    else:
        other_rows = conn.execute("""
            SELECT DISTINCT t.type_id, t.name
            FROM sde_types t
            JOIN sde_blueprint_products sbp ON sbp.product_type_id = t.type_id
            WHERE LOWER(t.name) LIKE ?
              AND sbp.activity IN ('manufacturing', 'reaction')
            ORDER BY t.name LIMIT 15
        """, [pattern]).fetchall()

    conn.close()
    return {
        "owned": owned,
        "other": [{"name": r[1], "type_id": r[0]} for r in other_rows],
    }


async def _bg_fetch_prices(type_ids: list[int]) -> None:
    """Fire-and-forget: fetch Jita prices for the given type_ids using a fresh connection."""
    import httpx as _httpx
    from app.market.prices import fetch_jita_prices_bulk as _bulk
    conn = get_conn()
    try:
        async with _httpx.AsyncClient() as client:
            await _bulk(client, conn, type_ids, force=True)
    except Exception:
        pass
    finally:
        conn.close()


def _refresh_type_ids(conn) -> list[int]:
    """Full set of type_ids to refresh — všechno obchodovatelné v EVE (market_group_id IS NOT NULL)
    plus user assets/blueprints/materials a aktuálně cachované typy.

    Tradeable filter pokrývá moduly, ammo, lodě, skillbooky, struktury atd. — vše,
    co lze koupit/prodat na marketu.
    """
    # Aggregate type IDs across ALL characters
    asset_type_ids: set[int] = set()
    bp_type_ids: set[int] = set()
    for char_id, _name in list_characters(conn):
        asset_type_ids |= {a["type_id"] for a in _load_assets_from_cache(conn, char_id)}
        bp_type_ids |= {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char_id)}
    mat_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT material_type_id FROM sde_blueprint_materials"
    ).fetchall()}
    cached_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM market_price_cache"
    ).fetchall()}
    # Všechny published tradeable typy (modules, ammo, ships, skillbooks, ...)
    tradeable_ids = {r[0] for r in conn.execute(
        "SELECT type_id FROM sde_types WHERE published=1 AND market_group_id IS NOT NULL"
    ).fetchall()}
    return list(asset_type_ids | bp_type_ids | mat_ids | cached_ids | tradeable_ids)


@app.post("/prices/refresh", response_class=HTMLResponse)
async def prices_refresh(request: Request):
    conn = get_conn()

    all_ids = _refresh_type_ids(conn)

    refreshed = await refresh_jita_prices_all(conn, all_ids)
    stats = get_price_cache_stats(conn)
    items = get_all_price_items(conn)
    conn.close()

    return _tr("prices.html", request, {
        "stats": stats,
        "refreshed_count": refreshed,
        "total_requested": len(all_ids),
        "items": items,
    })


@app.get("/prices/refresh/stream")
async def prices_refresh_stream():
    conn = get_conn()
    all_ids = _refresh_type_ids(conn)

    async def event_gen():
        try:
            async for chunk in stream_jita_refresh(conn, all_ids):
                yield chunk
        except Exception:
            pass
        finally:
            conn.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/prices/search")
async def prices_search(q: str = ""):
    if len(q.strip()) < 2:
        return {"mode": "name", "group_name": None, "items": []}
    conn = get_conn()
    await _ensure_groups_populated(conn)
    pattern = f"%{q.strip().lower()}%"
    import time as _time
    from app.market.prices import PRICE_CACHE_TTL
    now = _time.time()

    # Priorita: skupinový mód jen při PŘESNÉ shodě s názvem skupiny (např. "battleship"
    # → group "Battleship"). Pro libovolný podřetězec ("amarr") preferujeme name search,
    # protože uživatel hledá konkrétní typ podle názvu, ne všechny itemy z jedné z N
    # skupin obsahujících substring.
    group_rows = conn.execute(
        "SELECT group_id, name FROM sde_groups WHERE LOWER(name) = ? ORDER BY name",
        (q.strip().lower(),),
    ).fetchall()

    if group_rows:
        group_ids = [r[0] for r in group_rows]
        ph = ",".join("?" * len(group_ids))
        rows = conn.execute(f"""
            SELECT t.type_id, t.name, g.name AS group_name,
                   m.sell_price, m.buy_price, m.cached_at,
                   m.volume, m.jita_available
            FROM sde_types t
            JOIN sde_groups g ON g.group_id = t.group_id
            LEFT JOIN market_price_cache m ON m.type_id = t.type_id
            WHERE t.published = 1 AND t.group_id IN ({ph})
            ORDER BY g.name, t.name
            LIMIT 500
        """, group_ids).fetchall()
        found_groups = list(dict.fromkeys(r[1] for r in group_rows))
        label = ", ".join(found_groups[:3])

        # Ensure all returned types are tracked; background-fetch ones with no price yet.
        uncached = [r[0] for r in rows if r[5] is None]  # cached_at IS NULL → never fetched
        if uncached:
            conn.executemany(
                "INSERT OR IGNORE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,NULL,NULL,0)",
                [(tid,) for tid in uncached],
            )
            conn.commit()
            import asyncio as _asyncio
            _asyncio.create_task(_bg_fetch_prices(uncached))

        conn.close()
        return {
            "mode": "group",
            "group_name": label,
            "fetching_prices": len(uncached) > 0,
            "items": [
                {
                    "type_id":       r[0],
                    "name":          r[1],
                    "group_name":    r[2],
                    "sell_price":    r[3],
                    "buy_price":     r[4],
                    "fresh":         bool(r[5] and (now - r[5]) < PRICE_CACHE_TTL),
                    "volume":        r[6],
                    "jita_available": r[7],
                }
                for r in rows
            ],
        }

    # Fallback: hledání v názvech itemů. LEFT JOIN aby se zobrazily i typy bez ceny
    # v cache (právě dostáhneme na pozadí). Omezit jen na tradeable (market_group_id),
    # aby se nevracely BPC/nepublishované/věcí mimo trh.
    rows = conn.execute("""
        SELECT t.type_id, t.name, g.name AS group_name,
               m.sell_price, m.buy_price, m.cached_at,
               m.volume, m.jita_available
        FROM sde_types t
        LEFT JOIN sde_groups g ON g.group_id = t.group_id
        LEFT JOIN market_price_cache m ON m.type_id = t.type_id
        WHERE t.published = 1
          AND t.market_group_id IS NOT NULL
          AND LOWER(t.name) LIKE ?
        ORDER BY t.name
        LIMIT 100
    """, (pattern,)).fetchall()

    # Bg-fetch pro typy bez ceny
    uncached = [r[0] for r in rows if r[5] is None]
    if uncached:
        conn.executemany(
            "INSERT OR IGNORE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,NULL,NULL,0)",
            [(tid,) for tid in uncached],
        )
        conn.commit()
        import asyncio as _asyncio
        _asyncio.create_task(_bg_fetch_prices(uncached))
    conn.close()
    return {
        "mode": "name",
        "group_name": None,
        "items": [
            {
                "type_id":       r[0],
                "name":          r[1],
                "group_name":    r[2],
                "sell_price":    r[3],
                "buy_price":     r[4],
                "fresh":         bool(r[5] and (now - r[5]) < PRICE_CACHE_TTL),
                "volume":        r[6],
                "jita_available": r[7],
            }
            for r in rows
        ],
    }


@app.post("/api/prices/custom")
async def api_set_custom_price(request: Request):
    body = await request.json()
    type_id = int(body["type_id"])
    price_raw = body.get("price")
    price = float(price_raw) if price_raw not in (None, "", "null") else None
    conn = get_conn()
    set_custom_price(conn, type_id, price)
    conn.close()
    return {"ok": True, "type_id": type_id, "price": price}


@app.post("/api/prices/station-volume")
async def api_station_volume(request: Request):
    body = await request.json()
    location_id = int(body["location_id"])

    conn = get_conn()
    token = get_active_token(request, conn)
    ensure_price_table(conn)

    # Zkus cache
    cached = get_cached_station_volumes(conn, location_id)
    if cached is not None:
        conn.close()
        return {"ok": True, "cached": True, "data": {
            str(k): {"volume": v[0], "best_sell": v[1], "traded_volume": v[2]}
            for k, v in cached.items()
        }}

    type_ids = [r[0] for r in conn.execute("SELECT type_id FROM market_price_cache").fetchall()]
    if not type_ids:
        type_ids = _refresh_type_ids(conn)

    def _fmt(result):
        return {"ok": True, "cached": False, "data": {
            str(k): {"volume": v[0], "best_sell": v[1], "traded_volume": v[2]}
            for k, v in result.items()
        }}

    # Player struktura (Upwell citadela, Fortizar, …) — použij strukturový market endpoint
    if location_id >= 1_000_000_000:
        if not token:
            conn.close()
            return {"ok": False, "error": "Pro přístup k marketu struktury je nutné přihlášení."}
        region_id = await get_region_for_location(conn, location_id, token)
        try:
            result = await fetch_structure_market(conn, location_id, token, set(type_ids), region_id)
        except PermissionError as e:
            conn.close()
            return {"ok": False, "error": str(e)}
        conn.close()
        return _fmt(result)

    # NPC stanice — regionální veřejný endpoint
    region_id = await get_region_for_location(conn, location_id, token)
    if not region_id:
        conn.close()
        return {"ok": False, "error": "Nepodařilo se určit region pro tuto lokaci."}

    result = await fetch_station_volumes(conn, location_id, region_id, type_ids)
    conn.close()
    return _fmt(result)


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return _tr("about.html", request, {"version": APP_VERSION})


# ── Version check / update ───────────────────────────────────────────────────

_GITHUB_REPO = "ScoopEMPRetro/Eve-retroindustry"
_VERSION_CACHE: dict | None = None
_VERSION_CACHE_TS: float = 0.0
_VERSION_CACHE_TTL = 3600.0  # 1 hour


def _is_bundled() -> bool:
    return hasattr(_sys, "_MEIPASS")


def _app_dir() -> Path:
    return Path(os.environ.get("EVE_APP_DIR", "."))


@app.get("/api/version/check")
async def api_version_check():
    global _VERSION_CACHE, _VERSION_CACHE_TS
    now = _time.monotonic()
    if _VERSION_CACHE and (now - _VERSION_CACHE_TS) < _VERSION_CACHE_TTL:
        return _VERSION_CACHE
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "EVE-Retroindustry"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return {"error": str(exc), "current": APP_VERSION}

    latest_tag = data.get("tag_name", "").lstrip("v")
    has_update = bool(latest_tag) and latest_tag != APP_VERSION

    plat = "win64" if _sys.platform == "win32" else "linux"
    asset_name = f"EVE_Retroindustry-v{latest_tag}-{plat}.zip"
    download_url = next(
        (a["browser_download_url"] for a in data.get("assets", []) if a["name"] == asset_name),
        None,
    )

    result = {
        "current": APP_VERSION,
        "latest": latest_tag,
        "has_update": has_update,
        "download_url": download_url,
        "release_url": data.get("html_url", ""),
        "release_name": data.get("name", f"v{latest_tag}"),
        "bundled": _is_bundled(),
    }
    _VERSION_CACHE = result
    _VERSION_CACHE_TS = now
    return result


@app.get("/api/version/download")
async def api_version_download(url: str):
    """SSE stream: downloads and extracts update zip to update_staging/ next to the exe."""
    if not (url.startswith("https://github.com/") or url.startswith("https://objects.githubusercontent.com/")):
        async def _err():
            yield f"data: {json.dumps({'error': 'Invalid download URL'})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def _stream():
        app_dir = _app_dir()
        staging = app_dir / "update_staging"
        tmp_zip = app_dir / "update.zip.tmp"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        yield f"data: {json.dumps({'error': f'HTTP {r.status_code}'})}\n\n"
                        return
                    total = int(r.headers.get("content-length", 0))
                    downloaded = 0
                    with open(tmp_zip, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            pct = int(downloaded * 100 / total) if total else 0
                            yield f"data: {json.dumps({'phase': 'download', 'pct': pct, 'downloaded': downloaded, 'total': total})}\n\n"

            yield f"data: {json.dumps({'phase': 'extract', 'pct': 0})}\n\n"

            import shutil
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)

            with _zipfile.ZipFile(tmp_zip) as zf:
                members = zf.namelist()
                total_files = len(members)
                for i, member in enumerate(members):
                    zf.extract(member, staging)
                    pct = int((i + 1) * 100 / total_files) if total_files else 100
                    if i % 50 == 0 or i == total_files - 1:
                        yield f"data: {json.dumps({'phase': 'extract', 'pct': pct})}\n\n"

            tmp_zip.unlink(missing_ok=True)

            # Detect single root subdirectory (EVE_Retroindustry/) inside the zip
            roots = {Path(m).parts[0] for m in members if Path(m).parts}
            inner_dir = staging / roots.pop() if len(roots) == 1 else staging

            # Write helper script
            if _sys.platform == "win32":
                script_path = app_dir / "update.bat"
                script_path.write_text(
                    f'@echo off\r\n'
                    f'timeout /t 3 /nobreak >nul\r\n'
                    f'xcopy /E /Y /I "{inner_dir}\\*" "{app_dir}\\"\r\n'
                    f'rmdir /S /Q "{staging}"\r\n'
                    f'del "%~f0"\r\n'
                    f'start "" "{app_dir}\\EVE_Retroindustry.exe"\r\n',
                    encoding="utf-8",
                )
            else:
                script_path = app_dir / "update.sh"
                script_path.write_text(
                    f'#!/bin/bash\n'
                    f'sleep 3\n'
                    f'cp -r "{inner_dir}/." "{app_dir}/"\n'
                    f'rm -rf "{staging}"\n'
                    f'chmod +x "{app_dir}/EVE_Retroindustry"\n'
                    f'"{app_dir}/EVE_Retroindustry" &\n'
                    f'rm -- "$0"\n',
                    encoding="utf-8",
                )
                import stat
                script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

            yield f"data: {json.dumps({'done': True, 'script': script_path.name})}\n\n"

        except Exception as exc:
            if tmp_zip.exists():
                tmp_zip.unlink(missing_ok=True)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/version/apply")
async def api_version_apply():
    """Launch helper update script then exit the process."""
    import subprocess
    app_dir = _app_dir()
    if _sys.platform == "win32":
        script = app_dir / "update.bat"
    else:
        script = app_dir / "update.sh"
    if not script.exists():
        return {"error": f"{script.name} not found — run download first"}
    if _sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True, close_fds=True)
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"ok": True}
