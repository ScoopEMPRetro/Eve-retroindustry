#!/usr/bin/env python3
"""Regenerate the README screenshots from anonymized demo data.

Takes the local (real) eve_cache.db, builds an anonymized copy — fake pilot
names/ids, fake corporations, generated wallet balances, renamed private
structures — then starts the app against that copy and screenshots the
Dashboard, Production Plan and Assets pages into docs/screenshots/.

Nothing traceable to the real account ends up in the images: item/blueprint
structure is kept so the screenshots look realistic, but every identifying
value is replaced.

Requirements: the project venv, `chromium`, and ImageMagick (`magick`).
Usage:  venv/bin/python scripts/gen_demo_screenshots.py
"""
from __future__ import annotations
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from fastapi import Request  # module-level so FastAPI can resolve the route annotation

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DB = os.path.join(REPO, "eve_cache.db")
OUT_DIR = os.path.join(REPO, "docs", "screenshots")
PORT = 8899

# Generated pilot identities. character_id must look like a real player id
# (>= 90,000,000) so the EVE image server serves a portrait; the ids/corps below
# resolve to random unrelated players, never the real account.
FAKE_CHARS = [
    (2119911001, "Rethvann Okaski", 98000101),
    (2119911002, "Sella Draik",     98000101),
    (2119911003, "Corwin Vael",     98000202),
    (2119911004, "Nyx Tarreno",     98000202),
    (2119911005, "Halva Merrik",    98000303),
    (2119911006, "Ordo Kesh",       98000303),
]
FAKE_WALLETS = [4_812_663_441.22, 1_205_337_910.05, 22_984_110_662.80,
                318_774_205.61, 7_640_552_318.44, 96_331_770.19]
STATIONS = [60003760, 60008494, 60011866]  # Jita, Amarr, Dodixie (NPC, in SDE)


def build_demo_db(dst_dir: str) -> str:
    out_db = os.path.join(dst_dir, "eve_cache.db")
    shutil.copy2(SRC_DB, out_db)
    conn = sqlite3.connect(out_db)

    def cols(t):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}

    def has(t):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone() is not None

    real = conn.execute(
        "SELECT character_id, corporation_id FROM characters ORDER BY added_at ASC"
    ).fetchall()
    if not real:
        raise SystemExit("no characters in source DB")

    fakes = list(FAKE_CHARS)
    while len(fakes) < len(real):
        k = len(fakes)
        fakes.append((2119911001 + k, f"Pilot {k+1:02d}", 98000900 + k))

    id_map, corp_map = {}, {}
    for (rid, rcorp), (fid, fname, fcorp) in zip(real, fakes):
        id_map[rid] = (fid, fname, fcorp)
        if rcorp:
            corp_map[rcorp] = fcorp

    now = time.time()
    conn.execute("DELETE FROM characters")
    for i, (rid, _) in enumerate(real):
        fid, fname, fcorp = id_map[rid]
        conn.execute(
            """INSERT INTO characters (character_id, character_name, refresh_token,
                access_token, token_expires_at, corporation_id, last_sync_at, added_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (fid, fname, "demo", "demo", now + 10**9, fcorp, now, now + i),
        )

    for t in ("char_assets_cache", "char_blueprints_cache",
              "char_skills_cache", "char_wallet_cache"):
        if not has(t):
            continue
        for rowid, cid in conn.execute(f"SELECT rowid, character_id FROM {t}").fetchall():
            if cid in id_map:
                conn.execute(f"UPDATE {t} SET character_id=? WHERE rowid=?",
                             (id_map[cid][0], rowid))
            else:
                conn.execute(f"DELETE FROM {t} WHERE rowid=?", (rowid,))

    if has("char_wallet_cache"):
        for i, (rid, _) in enumerate(real):
            conn.execute("UPDATE char_wallet_cache SET balance=? WHERE character_id=?",
                         (FAKE_WALLETS[i % len(FAKE_WALLETS)], id_map[rid][0]))

    if has("corp_assets_cache"):
        for rowid, corp in conn.execute("SELECT rowid, corporation_id FROM corp_assets_cache").fetchall():
            if corp in corp_map:
                conn.execute("UPDATE corp_assets_cache SET corporation_id=? WHERE rowid=?",
                             (corp_map[corp], rowid))
            else:
                conn.execute("DELETE FROM corp_assets_cache WHERE rowid=?", (rowid,))

    if has("location_name_cache"):
        generic = ["Engineering Complex", "Refinery", "Keepstar", "Fortizar",
                   "Astrahus", "Raitaru", "Azbel", "Sotiyo", "Tatara", "Athanor"]
        gi = 0
        for lid, _ in conn.execute("SELECT location_id, name FROM location_name_cache").fetchall():
            if lid and lid >= 1_000_000_000_000:   # player structure id range
                conn.execute("UPDATE location_name_cache SET name=? WHERE location_id=?",
                             (f"{generic[gi % len(generic)]} — Demo {gi+1}", lid))
                gi += 1

    # Make every cache "fresh" so pages serve it without an ESI call.
    for t in ("char_assets_cache", "char_blueprints_cache", "char_skills_cache",
              "char_wallet_cache", "corp_assets_cache", "market_price_cache"):
        if has(t) and "cached_at" in cols(t):
            conn.execute(f"UPDATE {t} SET cached_at=?", (now,))

    conn.commit()
    conn.close()
    return out_db


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="eve-demo-")
    demo_db = build_demo_db(tmp)
    print("demo DB:", demo_db)

    os.environ["EVE_APP_DIR"] = tmp
    sys.path.insert(0, REPO)

    import uvicorn
    from fastapi.responses import HTMLResponse
    import app.web.main as m
    m._SDE_READY[0] = True

    # The dashboard reads current location + skill training live from ESI; with
    # the demo token those calls can't run, so stub them with generated values.
    skill_rows = sqlite3.connect(demo_db).execute(
        "SELECT type_id FROM sde_types WHERE name IN "
        "('Industry','Advanced Industry','Capital Ship Construction','Reactions')"
    ).fetchall()
    skill_ids = [r[0] for r in skill_rows] or [3380]
    order = {c[0]: i for i, c in enumerate(FAKE_CHARS)}
    utcnow = dt.datetime.now(dt.timezone.utc)
    finish = [(utcnow + dt.timedelta(days=2, hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              (utcnow + dt.timedelta(hours=9, minutes=42)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              (utcnow + dt.timedelta(days=5, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")]

    async def fake_location(client, cid, tok):
        return {"station_id": STATIONS[order.get(cid, 0) % len(STATIONS)]}

    async def fake_skill_queue(client, cid, tok):
        i = order.get(cid, 0)
        fin = finish[i % len(finish)]
        # Give the active entry SP + a past start_date so SP/hour can be derived
        # (rate = sp / total_hours). Pick a plausible rate per character.
        rate = [2700, 2340, 1980][i % 3]
        total_h = [55, 20, 40][i % 3]
        fin_dt = dt.datetime.strptime(fin, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        start_dt = fin_dt - dt.timedelta(hours=total_h)
        return [{"skill_id": skill_ids[i % len(skill_ids)],
                 "finished_level": [5, 4, 3][i % 3],
                 "finish_date": fin,
                 "start_date": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "level_start_sp": 0, "training_start_sp": 0,
                 "level_end_sp": round(rate * total_h)}]

    m.fetch_location = fake_location
    m.fetch_skill_queue = fake_skill_queue

    # ── Jobs & Orders are fetched live from ESI (never cached in the DB), so —
    #    like location/skills above — stub the fetchers with a realistic spread. ──
    _c = sqlite3.connect(demo_db)
    TID = {}
    for _n in ("Raven", "Scorpion", "Megathron", "Tempest",
               "Scourge Fury Heavy Missile", "Ferrogel", "Tritanium", "Large Skill Injector"):
        _r = _c.execute("SELECT type_id FROM sde_types WHERE name=?", (_n,)).fetchone()
        TID[_n] = _r[0] if _r else None
    _c.close()

    def _iso(delta):
        return (utcnow + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _job(jid, name, activity, runs, ends, status, facility):
        return {"job_id": jid, "product_type_id": TID[name], "blueprint_type_id": None,
                "facility_id": facility, "activity_id": activity, "runs": runs,
                "status": status, "cost": 0.0,
                "start_date": _iso(dt.timedelta(hours=-8)), "end_date": _iso(ends)}

    JOBS_BY_INDEX = {
        0: [_job(9001, "Raven", 1, 1, dt.timedelta(days=2, hours=7), "active", STATIONS[0]),
            _job(9002, "Scourge Fury Heavy Missile", 1, 250, dt.timedelta(hours=9, minutes=20), "active", STATIONS[0]),
            _job(9003, "Ferrogel", 9, 40, dt.timedelta(hours=-1), "ready", STATIONS[0])],
        1: [_job(9004, "Megathron", 1, 2, dt.timedelta(days=5, hours=3), "active", STATIONS[1]),
            _job(9005, "Tempest", 1, 1, dt.timedelta(hours=14), "active", STATIONS[1])],
        2: [_job(9006, "Scorpion", 1, 1, dt.timedelta(hours=3, minutes=40), "active", STATIONS[2])],
    }

    async def fake_industry_jobs(client, cid, tok, include_completed=True):
        return list(JOBS_BY_INDEX.get(order.get(cid, 0), []))

    m.jobs_api.fetch_industry_jobs = fake_industry_jobs

    def _ord(name, total, remain, price, is_buy, duration, facility):
        return {"type_id": TID[name], "volume_total": total, "volume_remain": remain,
                "price": price, "is_buy_order": is_buy, "location_id": facility,
                "issued": _iso(dt.timedelta(days=-2)), "duration": duration}

    ORDERS_BY_INDEX = {
        0: [_ord("Raven", 4, 2, 224_900_000.0, False, 90, STATIONS[0]),
            _ord("Tempest", 3, 3, 78_500_000.0, False, 90, STATIONS[0]),
            _ord("Tritanium", 80_000_000, 41_320_540, 5.62, True, 30, STATIONS[0])],
        1: [_ord("Megathron", 2, 1, 191_800_000.0, False, 90, STATIONS[1]),
            _ord("Large Skill Injector", 6, 6, 792_400_000.0, False, 14, STATIONS[1])],
        2: [_ord("Scorpion", 5, 4, 68_200_000.0, False, 90, STATIONS[2])],
    }

    async def fake_orders(client, cid, tok):
        return list(ORDERS_BY_INDEX.get(order.get(cid, 0), []))

    m.orders_api.fetch_orders = fake_orders

    @m.app.get("/demo/assetsshot")
    async def _assetsshot(request: Request):
        # Render the real assets page, then expand the first station + its hangar
        # so the screenshot shows the container/ship drill-down.
        resp = await m.assets_page(request, view="all")
        html = resp.template.render(resp.context)   # TemplateResponse isn't rendered yet
        # Pick the first station that holds a container (i.e. has ships), move it
        # to the top, expand it so the Hangar + ship/container rows show, and open
        # one ship's contents. Only open the hangar table if it's small (avoids
        # rendering a several-hundred-row table that would blow the screenshot).
        inject = (
            "<script>addEventListener('load',function(){setTimeout(function(){"
            "var list=document.getElementById('stations-list'); if(!list)return;"
            "var cards=[].slice.call(list.children);"
            "var pick=null;"
            "for(var i=0;i<cards.length;i++){if(cards[i].querySelector&&cards[i].querySelector('.collapse[id^=\"cont-\"]')){pick=cards[i];break;}}"
            "if(!pick)pick=cards[0]; if(!pick)return;"
            "list.insertBefore(pick,list.firstChild);"
            "var st=pick.querySelector('.collapse[id^=\"st-\"]'); if(st)st.classList.add('show');"
            "var cont=pick.querySelector('.collapse[id^=\"cont-\"]'); if(cont)cont.classList.add('show');"
            "var hng=pick.querySelector('.collapse[id^=\"hng-\"]');"
            "if(hng){var hd=hng.previousElementSibling;var b=hd&&hd.querySelector('.badge');"
            "var n=b?parseInt(b.textContent):999; if(n<=40)hng.classList.add('show');}"
            "},350);});</script>")
        return HTMLResponse(html.replace("</body>", inject + "</body>"))

    @m.app.get("/demo/planshot")
    async def _planshot():
        return HTMLResponse(
            '<!doctype html><body style="background:#0d1117">'
            '<form id="f" method="post" action="/plan">'
            '<input name="product" value="Raven"><input name="station" value="60003760">'
            '<input name="qty" value="1"><input name="mode" value="full">'
            '<input name="form_me" value="10"><input name="form_te" value="20">'
            '<input name="selling_station" value="60003760">'
            '</form><script>document.getElementById("f").submit()</script>')

    @m.app.get("/demo/pricesshot")
    async def _pricesshot(request: Request):
        # Render the real Prices page, then type "Battleship" into the search so the
        # table shows that market group (all battleship hulls have cached prices).
        resp = await m.prices_page(request)
        html = resp.template.render(resp.context)
        inject = (
            "<script>addEventListener('load',function(){setTimeout(function(){"
            "var s=document.getElementById('price-search');"
            "if(s){s.value='Battleship';s.dispatchEvent(new Event('input',{bubbles:true}));}"
            "},500);});</script>")
        return HTMLResponse(html.replace("</body>", inject + "</body>"))

    cfg = uvicorn.Config(m.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()

    import httpx
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)

    shots = {
        "dashboard":       f"http://127.0.0.1:{PORT}/",
        "production-plan": f"http://127.0.0.1:{PORT}/demo/planshot",
        "assets":          f"http://127.0.0.1:{PORT}/demo/assetsshot",
        "jobs":            f"http://127.0.0.1:{PORT}/jobs",
        "orders":          f"http://127.0.0.1:{PORT}/orders?char=all",
        "prices":          f"http://127.0.0.1:{PORT}/demo/pricesshot",
    }
    HEIGHT = {"assets": 1750}
    VTB = {"assets": 13000, "prices": 12000}
    for name, url in shots.items():
        out = os.path.join(OUT_DIR, f"{name}.png")
        h = HEIGHT.get(name, 1500)
        vtb = VTB.get(name, 9000)
        prof = os.path.join(tmp, f"chrome-{name}")
        subprocess.run(
            ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-first-run", f"--user-data-dir={prof}",
             "--hide-scrollbars", "--force-device-scale-factor=2",
             f"--virtual-time-budget={vtb}", f"--window-size=1600,{h}",
             f"--screenshot={out}", url],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["magick", out, "-fuzz", "3%", "-trim", "+repage", out],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("wrote", out)

    shutil.rmtree(tmp, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    main()
