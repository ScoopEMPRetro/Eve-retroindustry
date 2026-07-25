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


def test_plan_contract_price_requires_login(client):
    # No active-character cookie -> not signed in / graceful error, never a 500.
    r = client.get("/api/plan/contract-price",
                   params={"location_id": 60003760, "type_id": 34})
    assert r.status_code == 200
    assert r.json().get("ok") is not True
