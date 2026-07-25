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
        return [{"skill_id": skill_ids[i % len(skill_ids)],
                 "finished_level": [5, 4, 3][i % 3],
                 "finish_date": finish[i % len(finish)]}]

    m.fetch_location = fake_location
    m.fetch_skill_queue = fake_skill_queue

    @m.app.get("/demo/planshot")
    async def _planshot():
        return HTMLResponse(
            '<!doctype html><body style="background:#0d1117">'
            '<form id="f" method="post" action="/plan">'
            '<input name="product" value="Hyperion"><input name="station" value="60003760">'
            '<input name="qty" value="1"><input name="mode" value="full">'
            '<input name="selling_station" value="60003760">'
            '</form><script>document.getElementById("f").submit()</script>')

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
        "assets":          f"http://127.0.0.1:{PORT}/assets?view=all",
    }
    for name, url in shots.items():
        out = os.path.join(OUT_DIR, f"{name}.png")
        h = 1750 if name == "assets" else 1500
        subprocess.run(
            ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=2",
             "--virtual-time-budget=9000", f"--window-size=1600,{h}",
             f"--screenshot={out}", url],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["magick", out, "-fuzz", "3%", "-trim", "+repage", out],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("wrote", out)

    shutil.rmtree(tmp, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    main()
