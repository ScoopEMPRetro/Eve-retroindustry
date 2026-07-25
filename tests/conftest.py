"""Test fixtures: a synthetic app instance backed by the bundled SDE.

The DB is built from the committed ``sde_base.db`` (available in CI) plus a
little synthetic user data, so tests never touch the real ``eve_cache.db`` and
need no network. Live ESI fetchers used during page rendering are stubbed.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NPC_STATION = 60003760           # Jita IV-4 (resolves from the SDE, no ESI)
JITA_SYSTEM = 30000142


def _seed(m) -> None:
    conn = m.get_conn()          # ensures the user tables exist
    try:
        now = time.time()
        rows = conn.execute(
            "SELECT type_id, name FROM sde_types WHERE type_id IN (34,35,36,37,38)"
        ).fetchall()
        names = {r[0]: r[1] for r in rows}
        ids = sorted(names) or [34]
        chars = [(900000001, "Test Pilot Alpha", 98000001),
                 (900000002, "Test Pilot Beta", 98000001)]
        conn.execute("DELETE FROM characters")
        for i, (cid, nm, corp) in enumerate(chars):
            conn.execute(
                "INSERT INTO characters (character_id, character_name, refresh_token, "
                "access_token, token_expires_at, corporation_id, last_sync_at, added_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, nm, "test", "test", now + 10**9, corp, now, now + i))
            conn.execute(
                "INSERT OR REPLACE INTO char_wallet_cache (character_id, balance, cached_at) "
                "VALUES (?,?,?)", (cid, 1_000_000_000.0 * (i + 1), now))
            assets = [{"item_id": 1000 + j, "type_id": t, "quantity": 10_000,
                       "location_id": NPC_STATION, "location_flag": "Hangar",
                       "is_singleton": False, "name": names.get(t, str(t)),
                       "solar_system_id": JITA_SYSTEM} for j, t in enumerate(ids)]
            conn.execute(
                "INSERT OR REPLACE INTO char_assets_cache (character_id, data_json, cached_at) "
                "VALUES (?,?,?)", (cid, json.dumps(assets), now))
            conn.execute(
                "INSERT OR REPLACE INTO char_blueprints_cache (character_id, data_json, cached_at) "
                "VALUES (?,?,?)", (cid, json.dumps([]), now))
        for t in ids:
            conn.execute(
                "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) "
                "VALUES (?,?,?,?)", (t, 5.0, 4.0, now))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def app_module():
    tmp = tempfile.mkdtemp(prefix="eve-test-")
    shutil.copy2(os.path.join(REPO, "sde_base.db"), os.path.join(tmp, "eve_cache.db"))
    os.environ["EVE_APP_DIR"] = tmp
    os.environ["EVE_BUNDLE_DIR"] = REPO
    if REPO not in sys.path:
        sys.path.insert(0, REPO)

    import app.web.main as m
    m._SDE_READY[0] = True
    _seed(m)

    # Stub live-only ESI fetchers so rendering is hermetic and fast.
    async def _loc(client, cid, tok):
        return {"station_id": NPC_STATION}

    async def _sq(client, cid, tok):
        return []

    m.fetch_location = _loc
    m.fetch_skill_queue = _sq
    # Never actually launch a browser from a test.
    m._open_in_external_browser = lambda url: True

    yield m
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)
