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


def test_plan_contract_price_requires_login(client):
    # No active-character cookie -> not signed in / graceful error, never a 500.
    r = client.get("/api/plan/contract-price",
                   params={"location_id": 60003760, "type_id": 34})
    assert r.status_code == 200
    assert r.json().get("ok") is not True
