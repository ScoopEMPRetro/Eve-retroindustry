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


def test_plan_contract_price_requires_login(client):
    # No active-character cookie -> not signed in / graceful error, never a 500.
    r = client.get("/api/plan/contract-price",
                   params={"location_id": 60003760, "type_id": 34})
    assert r.status_code == 200
    assert r.json().get("ok") is not True
