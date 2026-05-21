"""FastAPI web aplikace pro EVE Retroindustry."""
from __future__ import annotations

APP_VERSION = "0.2.11"

import asyncio
import datetime
import os
import json
import sqlite3
import threading
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.auth.token_store import get_valid_token, get_character, is_logged_in
from app.auth.esi_oauth import start_web_login
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
    derive_facility_tax,
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


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    """Redirect every request to /setup until SDE data is available."""
    if not _SDE_READY[0] and not request.url.path.startswith("/setup"):
        return RedirectResponse("/setup")
    return await call_next(request)


@app.on_event("startup")
async def _startup_populate_groups():
    """Check SDE readiness, load group names and rig bonuses."""
    try:
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
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


templates.env.filters["isk"] = _isk
templates.env.filters["format_number"] = _format_number
templates.env.filters["format_date"] = _format_date


def _tr(name: str, request: Request, context: dict) -> HTMLResponse:
    """Starlette nové API: request jako první argument."""
    context.setdefault("character", get_character())
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sde_groups (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _science_skill_mult(
    conn: sqlite3.Connection,
    bp_type_id: int,
    activity: str,
    skills: dict[int, int],
) -> tuple[float, list[tuple[str, int, float]]]:
    """Vrátí (multiplier, [(skill_name, level, bonus_pct), ...]) pro science skilly blueprintu.

    Každý required skill s time bonusem přispívá (1 - level * bonus_pct/100).
    Industry a AdvIndustry jsou zpracovány zvlášť — zde je přeskakujeme.
    """
    try:
        rows = conn.execute(
            """SELECT bs.skill_type_id, st.skill_name, bs.required_level, st.time_bonus_pct
               FROM sde_blueprint_skills bs
               JOIN sde_skill_time_bonus st ON st.skill_type_id = bs.skill_type_id
               WHERE bs.blueprint_type_id = ? AND bs.activity = ?""",
            (bp_type_id, activity),
        ).fetchall()
    except Exception:
        return 1.0, []

    mult = 1.0
    details: list[tuple[str, int, float]] = []
    for skill_id, skill_name, _req_level, bonus_pct in rows:
        level = skills.get(skill_id, 0)
        mult *= 1.0 - level * bonus_pct / 100
        details.append((skill_name, level, bonus_pct))
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

@app.get("/auth/login")
async def auth_login():
    _sync_state["done"] = False
    url = start_web_login()
    if not url:
        return RedirectResponse("/?login_busy=1")
    return RedirectResponse(url)


async def _bg_initial_sync():
    """Fetch blueprints + personal + corp assets from ESI after login."""
    try:
        token = get_valid_token()
        char = get_character()
        if not token or not char:
            return
        char_id, _ = char
        conn = get_conn()
        async with httpx.AsyncClient() as client:
            await fetch_blueprints(client, char_id, token, conn)
            all_assets = await fetch_assets(client, char_id, token, conn)
            await fetch_skills(client, char_id, token, conn)
            try:
                _, corp_assets = await fetch_corp_assets(client, char_id, token, conn)
            except Exception:
                corp_assets = []

        personal_loc_ids = {a.location_id for a in all_assets}
        corp_loc_ids = {a.location_id for a in corp_assets} - personal_loc_ids
        all_loc_ids = list(personal_loc_ids | corp_loc_ids)
        if all_loc_ids:
            await resolve_station_names_bulk(all_loc_ids, token=token, conn=conn)

        conn.close()
    finally:
        _sync_state["running"] = False
        _sync_state["done"] = True


@app.get("/auth/sync", response_class=HTMLResponse)
async def auth_sync(request: Request):
    if not is_logged_in():
        return RedirectResponse("/")
    if not _sync_state["running"] and not _sync_state["done"]:
        _sync_state["running"] = True
        _sync_state["done"] = False
        asyncio.create_task(_bg_initial_sync())
    return _tr("sync.html", request, {})


@app.get("/api/sync-status")
async def api_sync_status():
    return {"done": _sync_state["done"], "running": _sync_state["running"]}


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
    logged_in = is_logged_in()
    char_name = char_id = None
    bp_count = asset_locations = 0
    total_assets_value = None
    price_stats = {}

    conn = get_conn()
    if logged_in:
        char = get_character()
        if char:
            char_id, char_name = char

        if char_id:
            row = conn.execute(
                "SELECT json_array_length(data_json) FROM char_blueprints_cache WHERE character_id=?",
                (char_id,)
            ).fetchone()
            bp_count = row[0] if row and row[0] else 0

        total_assets_value = None
        if char_id:
            raw = _load_assets_from_cache(conn, char_id)
            non_singletons = [a for a in raw if not a.get("is_singleton", False)]
            locs = {a["location_id"] for a in non_singletons}
            asset_locations = len(locs)

            all_type_ids = list({a["type_id"] for a in non_singletons})
            if all_type_ids:
                prices = await get_prices_for_ids(conn, all_type_ids)
                total_assets_value = sum(
                    prices.get(a["type_id"], (None, None))[0] * a.get("quantity", 1)
                    for a in non_singletons
                    if prices.get(a["type_id"], (None, None))[0] is not None
                )

        price_stats = get_price_cache_stats(conn)

    conn.close()
    return _tr("index.html", request, {
        "logged_in": logged_in,
        "char_name": char_name,
        "char_id": char_id,
        "bp_count": bp_count,
        "asset_locations": asset_locations,
        "total_assets_value": total_assets_value,
        "price_stats": price_stats,
        "login_busy": request.query_params.get("login_busy") == "1",
    })


# ---------------------------------------------------------------------------
# Výrobní plán
# ---------------------------------------------------------------------------

@app.get("/plan", response_class=HTMLResponse)
async def plan_form(request: Request):
    conn = get_conn()
    char = get_character()
    token = get_valid_token()
    location_ids = []
    char_skills: dict[int, int] = {}
    if char:
        raw = _load_assets_from_cache(conn, char[0])
        location_ids = sorted({a["location_id"] for a in raw if not a.get("is_singleton", False)})
        if token:
            async with httpx.AsyncClient() as client:
                char_skills = await fetch_skills(client, char[0], token, conn)
        else:
            char_skills = get_cached_skills(conn, char[0])
    product_param = request.query_params.get("product", "")
    if product_param.strip().isdigit():
        row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (int(product_param),)).fetchone()
        if row:
            product_param = row[0]
    conn.close()
    return _tr("plan.html", request, {
        "locations": location_ids,
        "result": None,
        "error": None,
        "form_product": product_param,
        "form_industry":     str(char_skills.get(3380, 0)),
        "form_adv_industry": str(char_skills.get(3388, 0)),
    })


@app.post("/plan", response_class=HTMLResponse)
async def plan_result(
    request: Request,
    product: str = Form(...),
    station: int = Form(...),
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
):
    conn = get_conn()
    error = None
    plan_data = None

    # Převeď ME/TE na int pokud zadány
    me_override: int | None = int(form_me) if form_me.strip().isdigit() else None
    te_override: int | None = int(form_te) if form_te.strip().isdigit() else None

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
        token = get_valid_token()
        char = get_character()
        if not token or not char:
            raise ValueError("Nejsi přihlášen.")
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

        # Sbírám unikátní science skilly ze všech jobů pro zobrazení v headeru
        _seen: dict[str, tuple[int, float]] = {}
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                for sname, slevel, spct in job.get("science_skills", []):
                    if sname not in _seen:
                        _seen[sname] = (slevel, spct)
        plan_data["all_science_skills"] = [
            (n, l, p) for n, (l, p) in sorted(_seen.items())
        ]

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

    char2 = get_character()
    location_ids = []
    if char2:
        raw = _load_assets_from_cache(conn, char2[0])
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
        "form_me": str(int(me)) if me_override is not None else "",
        "form_te": str(te) if te_override is not None else "",
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
async def assets_page(request: Request, search: str = ""):
    conn = get_conn()
    token = get_valid_token()
    char = get_character()
    stations: list[dict] = []
    corp_stations: list[dict] = []

    if char and token:
        char_id, _ = char
        async with httpx.AsyncClient() as client:
            all_assets = await fetch_assets(client, char_id, token, conn)
            try:
                corp_id, corp_assets_list = await fetch_corp_assets(client, char_id, token, conn)
            except Exception:
                corp_id, corp_assets_list = 0, []
            all_type_ids_for_names = list(
                {a.type_id for a in all_assets} | {a.type_id for a in corp_assets_list}
            )
            names = await resolve_names_bulk(conn, all_type_ids_for_names, client)

        # ── Personal assets ─────────────────────────────────────────────────
        parent_map: dict[int, int] = {a.item_id: a.location_id for a in all_assets}
        asset_item_ids = {a.item_id for a in all_assets}

        def _hierarchy(a) -> tuple[int, int | None]:
            loc = a.location_id
            if loc not in asset_item_ids:
                return loc, None
            container_id = loc
            cur = loc
            seen: set[int] = set()
            while cur in asset_item_ids and cur not in seen:
                seen.add(cur)
                cur = parent_map.get(cur, cur)
                if cur not in asset_item_ids:
                    break
            return cur, container_id

        station_data: dict[int, dict] = {}

        def _get_st(sid: int) -> dict:
            if sid not in station_data:
                station_data[sid] = {"hangar": {}, "containers": {}}
            return station_data[sid]

        for a in all_assets:
            item_name = names.get(a.type_id, f"Unknown ({a.type_id})")
            if search and search.lower() not in item_name.lower():
                continue
            sid, cid = _hierarchy(a)
            st = _get_st(sid)
            bucket = st["hangar"] if cid is None else st["containers"].setdefault(cid, {})
            if a.type_id in bucket:
                bucket[a.type_id]["quantity"] += a.quantity
            else:
                bucket[a.type_id] = {
                    "type_id": a.type_id,
                    "name": item_name,
                    "quantity": a.quantity,
                    "is_blueprint_copy": a.is_blueprint_copy,
                }

        # ── Corporate assets ─────────────────────────────────────────────────
        # station_id → {div_flag → {"hangar": {type_id: item}, "containers": {cid: {type_id: item}}}}
        corp_sd: dict[int, dict] = {}
        if corp_assets_list:
            corp_item_ids = {a.item_id for a in corp_assets_list}
            corp_parent_map = {a.item_id: a.location_id for a in corp_assets_list}
            corp_flag_map = {a.item_id: a.location_flag for a in corp_assets_list}

            def _corp_hierarchy(a) -> tuple[int, str, int | None]:
                """Returns (station_id, division_flag, container_id|None)."""
                loc = a.location_id
                if loc not in corp_item_ids:
                    return loc, a.location_flag, None
                container_id = loc
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
                root_container = chain[-1] if chain else loc
                div_flag = corp_flag_map.get(root_container, "Hangar")
                return station_id, div_flag, container_id

            def _get_corp_div(sid: int, flag: str) -> dict:
                if sid not in corp_sd:
                    corp_sd[sid] = {}
                if flag not in corp_sd[sid]:
                    corp_sd[sid][flag] = {"hangar": {}, "containers": {}}
                return corp_sd[sid][flag]

            for a in corp_assets_list:
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
        all_price_ids = list({
            tid
            for sd in station_data.values()
            for tid in list(sd["hangar"]) + [t for c in sd["containers"].values() for t in c]
        } | {
            tid
            for sid_data in corp_sd.values()
            for dv in sid_data.values()
            for tid in list(dv["hangar"]) + [t for c in dv["containers"].values() for t in c]
        })
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
        assets_raw = _load_assets_from_cache(conn, char_id)
        container_info = await _resolve_container_names(char_id, token, all_container_ids, assets_raw) \
            if all_container_ids else {}
        container_type_map = {item["item_id"]: item["type_id"] for item in assets_raw}

        def _sort_items(bucket: dict) -> list:
            return sorted(bucket.values(), key=lambda x: x["name"])

        for sid, sd in station_data.items():
            containers = []
            for cid, items in sd["containers"].items():
                cname = container_info.get(cid, (f"Container {cid}", sid))[0]
                containers.append({
                    "container_id": cid,
                    "name": cname,
                    "type_id": container_type_map.get(cid),
                    "assets": _sort_items(items),
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
    })


@app.get("/api/assets/distances")
async def assets_distances():
    """Vrátí počet jumpů z aktuální pozice postavy ke každé lokaci v assets."""
    char = get_character()
    token = get_valid_token()
    if not char or not token:
        return {"ok": False, "error": "Nepřihlášen"}
    char_id, _ = char
    conn = get_conn()

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
async def blueprints_page(request: Request, search: str = ""):
    conn = get_conn()
    token = get_valid_token()
    char = get_character()
    bp_list = []

    if char and token:
        char_id, _ = char
        async with httpx.AsyncClient() as client:
            bps = await fetch_blueprints(client, char_id, token, conn)
            unique_ids = list({bp.type_id for bp in bps})
            names = await resolve_names_bulk(conn, unique_ids, client)

        bp_type_ids_list = list({bp.type_id for bp in bps})
        ph = ",".join("?" * len(bp_type_ids_list))
        prod_rows = conn.execute(
            f"SELECT blueprint_type_id, product_type_id FROM sde_blueprint_products"
            f" WHERE blueprint_type_id IN ({ph}) AND activity IN ('manufacturing','reaction')",
            bp_type_ids_list,
        ).fetchall() if bp_type_ids_list else []
        product_type_map = {r[0]: r[1] for r in prod_rows}

        for bp in sorted(bps, key=lambda b: names.get(b.type_id, "")):
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
            })

    from collections import defaultdict

    # Rozliš kontejnery od stanic pomocí assets cache
    assets = _load_assets_from_cache(conn, char_id if char else 0)
    asset_item_ids = {item["item_id"] for item in assets}

    all_raw_loc_ids = list({bp["location_id"] for bp in bp_list})
    container_ids = [lid for lid in all_raw_loc_ids if lid in asset_item_ids]
    structure_ids = [lid for lid in all_raw_loc_ids if lid not in asset_item_ids]

    # Resolvuj jména stanic
    loc_names = await resolve_station_names_bulk(structure_ids, token, conn) if structure_ids else {}

    # Resolvuj jména kontejnerů + jejich parent stanice
    container_info: dict[int, tuple[str, int]] = {}
    if container_ids and token and char:
        char_id, _ = char
        container_info = await _resolve_container_names(char_id, token, container_ids, assets)
        parent_ids_to_resolve = list({info[1] for info in container_info.values()
                                      if info[1] not in loc_names})
        if parent_ids_to_resolve:
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
    char = get_character()
    relevant: set[int] = set()
    if char:
        relevant |= {a["type_id"] for a in _load_assets_from_cache(conn, char[0])}
        relevant |= {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char[0])}
        # Také produkty vyrobitelné z user BPs (pro plánovací revenue)
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
async def station_industry_info(location_id: int):
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

    # Odvoď facility tax z jobů postavy
    facility_tax_pct: float | None = None
    tax_auto: bool = False
    token = get_valid_token()
    char  = get_character()
    if token and char:
        raw = await derive_facility_tax(conn, location_id, char[0], token)
        if raw is not None:
            facility_tax_pct = round(raw * 100, 2)
            tax_auto = True

    rig_info = get_station_rigs_full(conn, location_id)
    # ME bonus přepočítaný se security multiplierem (přepisuje stale stored value)
    me_bonus_live = get_station_me_bonus_pct(conn, location_id)
    conn.close()
    return {
        "solar_system_id":  solar_system_id,
        "security_status":  security_status,
        "mfg_sci":          mfg_sci,
        "rxn_sci":          rxn_sci,
        "facility_tax_pct": facility_tax_pct,
        "tax_auto":         tax_auto,
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
async def suggest_station(q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    ensure_location_name_table(conn)
    char = get_character()
    token = get_valid_token()
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
async def add_station(raw: str = Form(...)):
    """
    Přidá strukturu do cache. Přijímá:
    - ID struktury (číslo)
    - EVE URL formát: <url=showinfo:TYPE//ID>Jméno</url>
    - ID<mezera>Jméno: např. "1045667241057 C-N4OD - Fortizar"
    """
    import re
    conn = get_conn()
    ensure_location_name_table(conn)
    token = get_valid_token()

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
async def location_resolve(location_id: int):
    """Pokusí se dohledat jméno struktury přes ESI s aktuálním tokenem."""
    token = get_valid_token()
    if not token:
        return {"ok": False, "error": "Nepřihlášen"}
    conn = get_conn()
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
async def my_location():
    """Vrátí aktuální lokaci postavy (structure_id pokud je docknutá ve struktuře)."""
    token = get_valid_token()
    char = get_character()
    if not token or not char:
        return {"error": "Nepřihlášen"}

    conn = get_conn()
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
async def fetch_plan_sell_price(location_id: int, type_id: int):
    """Načte best sell cenu konkrétního produktu na zadané stanici, uloží do station_volume_cache."""
    token = get_valid_token()
    conn = get_conn()
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
async def suggest(q: str = ""):
    if len(q.strip()) < 2:
        return {"owned": [], "other": []}

    conn = get_conn()
    char = get_character()
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
    char = get_character()
    asset_type_ids: set[int] = set()
    bp_type_ids: set[int] = set()
    if char:
        asset_type_ids = {a["type_id"] for a in _load_assets_from_cache(conn, char[0])}
        bp_type_ids = {bp["type_id"] for bp in _load_blueprints_from_cache(conn, char[0])}
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
    token = get_valid_token()

    conn = get_conn()
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
