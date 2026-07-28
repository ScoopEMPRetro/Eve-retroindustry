"""API-endpoint tests (browser opener is stubbed in conftest)."""
import pytest


def test_support_open(client):
    d = client.get("/api/support/open").json()
    assert d["ok"] is True
    assert "ko-fi.com" in d["url"]


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/ScoopEMPRetro/Eve-retroindustry", True),
    ("https://esi.evetech.net/latest", True),
    ("https://ko-fi.com/retrovisor", True),
    ("https://evil.example.com/x", False),
    ("file:///etc/passwd", False),
    ("javascript:alert(1)", False),
    ("https://github.com.attacker.com/x", False),
])
def test_open_external_allowlist(client, url, ok):
    d = client.get("/api/open-external", params={"url": url}).json()
    assert d["ok"] is ok, d


def test_dashboard_renders_instantly_from_cache(client):
    # The dashboard must render from cache only (no ESI) so it can never hang;
    # the live-data placeholders confirm the ESI work was deferred.
    r = client.get("/")
    assert r.status_code == 200
    assert "Loading location" in r.text
    assert "/api/dashboard/live" in r.text


def test_dashboard_live_endpoint(client):
    d = client.get("/api/dashboard/live").json()
    assert d["logged_in"] is True
    # Both seeded characters are present.
    assert "900000001" in d["chars"] and "900000002" in d["chars"]
    c = d["chars"]["900000001"]
    # Wallet from the seeded cache; location from the stubbed ESI fetcher (Jita 4-4).
    assert c["wallet_str"]
    assert c["location_name"]


def test_token_refresh_is_serialized(app_module, monkeypatch):
    # Concurrent refreshes of the same character must not race on the rotating
    # refresh token: exactly one real refresh happens, nobody gets None.
    import threading
    from app.auth import token_store as ts
    m = app_module
    cid = 900000001

    c = m.get_conn()
    try:
        c.execute("UPDATE characters SET token_expires_at=0 WHERE character_id=?", (cid,))
        c.commit()
    finally:
        c.close()

    calls = {"n": 0}
    used: set[str] = set()
    guard = threading.Lock()

    class _Resp:
        def __init__(self, a, r, code=200, text=""):
            self.status_code, self.text, self._a, self._r = code, text, a, r
        def json(self):
            return {"access_token": self._a, "refresh_token": self._r, "expires_in": 1200}

    def fake_post(url, data=None, headers=None, timeout=None):
        with guard:
            calls["n"] += 1
            n = calls["n"]
            rt = (data or {}).get("refresh_token")
        if rt in used:                       # rotated token reused → EVE rejects
            return _Resp(None, None, 400, "invalid_grant")
        used.add(rt)
        return _Resp(f"acc-{n}", f"ref-{n}")

    monkeypatch.setattr(ts.httpx, "post", fake_post)

    results: list = []
    rlock = threading.Lock()

    def worker():
        cc = m.get_conn()
        try:
            tok = ts.get_valid_token(cc, cid)
        finally:
            cc.close()
        with rlock:
            results.append(tok)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert all(results), results          # nobody got None
        assert calls["n"] == 1, f"expected 1 real refresh, got {calls['n']}"
    finally:
        c = m.get_conn()
        try:
            c.execute("UPDATE characters SET access_token='test', refresh_token='test', "
                      "token_expires_at=? WHERE character_id=?", (2**31, cid))
            c.commit()
        finally:
            c.close()


def test_blueprint_badges_bpo_bpc_rxn(app_module, client):
    # The ESI assets flag is unreliable, so BPO/BPC come from the blueprints
    # endpoint (matched by item_id) and reaction formulas from sde_blueprints:
    #   - a copy (quantity -2)      → BPC badge, no price
    #   - an original (quantity -1) → BPO badge, priced
    #   - a reaction-formula original → RXN badge
    # A BPO and a BPC of the same type must not merge into one priced row.
    import json
    import time as _t
    m = app_module
    cid = 900000002
    orig_a = orig_b = None
    c = m.get_conn()
    try:
        mfg = c.execute(
            "SELECT blueprint_type_id FROM sde_blueprints WHERE manufacturing_time > 0 "
            "ORDER BY blueprint_type_id LIMIT 1"
        ).fetchone()
        rxn = c.execute(
            "SELECT blueprint_type_id FROM sde_blueprints WHERE reaction_time > 0 "
            "ORDER BY blueprint_type_id LIMIT 1"
        ).fetchone()
        assert mfg and rxn, "need a manufacturing and a reaction blueprint in the SDE"
        mfg_type, rxn_type = mfg[0], rxn[0]
        now = _t.time()
        orig_a = c.execute("SELECT data_json, cached_at FROM char_assets_cache WHERE character_id=?", (cid,)).fetchone()
        orig_b = c.execute("SELECT data_json, cached_at FROM char_blueprints_cache WHERE character_id=?", (cid,)).fetchone()

        # All three carry is_blueprint_copy=False (the unreliable asset flag).
        assets = [
            {"item_id": 777001, "type_id": mfg_type, "quantity": 1, "location_id": 60003760,
             "location_flag": "Hangar", "is_singleton": True, "is_blueprint_copy": False},
            {"item_id": 777002, "type_id": mfg_type, "quantity": 1, "location_id": 60003760,
             "location_flag": "Hangar", "is_singleton": True, "is_blueprint_copy": False},
            {"item_id": 777003, "type_id": rxn_type, "quantity": 1, "location_id": 60003760,
             "location_flag": "Hangar", "is_singleton": True, "is_blueprint_copy": False},
        ]
        bps = [
            {"item_id": 777001, "type_id": mfg_type, "location_id": 60003760, "location_flag": "Hangar",
             "quantity": -2, "runs": 10, "material_efficiency": 0, "time_efficiency": 0},   # BPC
            {"item_id": 777002, "type_id": mfg_type, "location_id": 60003760, "location_flag": "Hangar",
             "quantity": -1, "runs": -1, "material_efficiency": 0, "time_efficiency": 0},   # BPO
            {"item_id": 777003, "type_id": rxn_type, "location_id": 60003760, "location_flag": "Hangar",
             "quantity": -1, "runs": -1, "material_efficiency": 0, "time_efficiency": 0},   # RXN original
        ]
        # These caches have no UNIQUE(character_id), so mirror _save_cache: DELETE + INSERT.
        c.execute("DELETE FROM char_assets_cache WHERE character_id=?", (cid,))
        c.execute("INSERT INTO char_assets_cache (character_id, data_json, cached_at) VALUES (?,?,?)", (cid, json.dumps(assets), now))
        c.execute("DELETE FROM char_blueprints_cache WHERE character_id=?", (cid,))
        c.execute("INSERT INTO char_blueprints_cache (character_id, data_json, cached_at) VALUES (?,?,?)", (cid, json.dumps(bps), now))
        c.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, cached_at) VALUES (?,?,?,?)", (mfg_type, 750_000_000.0, 0.0, now))
        c.commit()
    finally:
        c.close()

    try:
        r = client.get(f"/assets?view={cid}")
        assert r.status_code == 200
        assert "badge-bpc" in r.text   # copy detected despite the asset flag
        assert "badge-bpo" in r.text   # original badged
        assert "badge-rxn" in r.text   # reaction formula badged distinctly
    finally:
        c = m.get_conn()
        try:
            c.execute("DELETE FROM char_assets_cache WHERE character_id=?", (cid,))
            if orig_a:
                c.execute("INSERT INTO char_assets_cache (character_id, data_json, cached_at) VALUES (?,?,?)", (cid, orig_a[0], orig_a[1]))
            c.execute("DELETE FROM char_blueprints_cache WHERE character_id=?", (cid,))
            if orig_b:
                c.execute("INSERT INTO char_blueprints_cache (character_id, data_json, cached_at) VALUES (?,?,?)", (cid, orig_b[0], orig_b[1]))
            c.commit()
        finally:
            c.close()


def test_prices_hub_columns_render(app_module, client):
    # A downloaded trade hub (Amarr / Domain) shows its comparison columns.
    import time as _t
    from app.market.prices import ensure_price_table
    m = app_module
    c = m.get_conn()
    try:
        ensure_price_table(c)
        c.execute(
            "INSERT OR REPLACE INTO hub_price_cache (region_id, type_id, sell_price, buy_price, volume, available, cached_at) "
            "VALUES (?,?,?,?,?,?,?)", (10000043, 34, 1234567.0, 5.5, 1234, 999, _t.time()))
        c.commit()
    finally:
        c.close()
    try:
        r = client.get("/prices")
        assert r.status_code == 200
        assert "Amarr sell" in r.text          # hub column header rendered
        assert "Amarr available" in r.text     # incl. per-hub available column
        assert "hub-fetch-btn" in r.text       # per-hub fetch buttons present
        assert "col-picker" in r.text          # metric/hub column picker present
        assert "1 234 567" in r.text           # European format, space thousands
        assert "1 234 567.00" not in r.text    # >=10k prices drop the decimals
        assert "1,234,567" not in r.text       # not American commas
        assert "5.00" in r.text                # cheap prices (<10k) keep decimals
        assert "Custom price" not in r.text    # custom price column removed
        assert "Prices last fetched" in r.text # freshness strip present
    finally:
        c = m.get_conn()
        try:
            c.execute("DELETE FROM hub_price_cache WHERE region_id=10000043")
            c.commit()
        finally:
            c.close()


def test_hub_refresh_unknown_region_404(client):
    r = client.get("/prices/refresh/hub/999/stream")
    assert r.status_code == 404


def test_price_history_endpoint(app_module, client):
    # Served from a fresh cache row → no ESI call needed (hermetic).
    import json
    import time as _t
    from app.market.prices import ensure_price_table, JITA_REGION
    m = app_module
    c = m.get_conn()
    try:
        ensure_price_table(c)
        series = [
            {"d": "2026-07-20", "avg": 100.0, "low": 95.0, "high": 110.0, "vol": 5000},
            {"d": "2026-07-21", "avg": 102.0, "low": 97.0, "high": 112.0, "vol": 6000},
        ]
        c.execute("INSERT OR REPLACE INTO price_history_cache (region_id, type_id, data_json, cached_at) "
                  "VALUES (?,?,?,?)", (JITA_REGION, 34, json.dumps(series), _t.time()))
        c.commit()
    finally:
        c.close()
    try:
        d = client.get("/api/prices/history?type_id=34").json()
        assert d["region_id"] == JITA_REGION
        assert len(d["series"]) == 2 and d["series"][0]["avg"] == 100.0
    finally:
        c = m.get_conn()
        try:
            c.execute("DELETE FROM price_history_cache WHERE type_id=34")
            c.commit()
        finally:
            c.close()


def test_prices_item_name_is_clickable(client):
    # The item name opens the history chart; the modal + hook must be present.
    r = client.get("/prices")
    assert r.status_code == 200
    assert 'class="item-hist"' in r.text
    assert 'id="histModal"' in r.text
    assert 'id="hist-market"' in r.text   # region/hub switcher present


def test_plan_contract_price_requires_login(client):
    # No active-character cookie -> not signed in / graceful error, never a 500.
    r = client.get("/api/plan/contract-price",
                   params={"location_id": 60003760, "type_id": 34})
    assert r.status_code == 200
    assert r.json().get("ok") is not True
