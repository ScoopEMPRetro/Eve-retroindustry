"""Unit tests for pure calculation logic (no DB / no network)."""
import sqlite3


# ── planner ────────────────────────────────────────────────────────────────
def test_format_duration():
    from app.manufacturing.planner import format_duration
    assert format_duration(0) == "—"
    assert format_duration(-5) == "—"
    assert format_duration(60) == "1m"
    assert format_duration(3600) == "1h"
    assert format_duration(3661) == "1h 1m"
    assert format_duration(90061) == "1d 1h 1m"
    assert format_duration(86400) == "1d"


def test_calc_job_time():
    from app.manufacturing.planner import calc_job_time
    # No bonuses → base time × runs.
    assert calc_job_time(1000, runs=1, te=0, industry_level=0, adv_industry_level=0) == 1000
    assert calc_job_time(1000, runs=3, te=0, industry_level=0, adv_industry_level=0) == 3000
    # Blueprint TE 10 % → ×0.9.
    assert calc_job_time(1000, runs=1, te=10, industry_level=0, adv_industry_level=0) == 900
    # Industry V (−20 %) applies to manufacturing but NOT reactions.
    assert calc_job_time(1000, 1, 0, 5, 0) == 800
    assert calc_job_time(1000, 1, 0, 5, 0, is_reaction=True) == 1000


# ── security multiplier ──────────────────────────────────────────────────────
def test_security_multiplier():
    from app.web.location_resolver import security_multiplier
    assert security_multiplier(None) == 1.0        # unknown → highsec fallback
    assert security_multiplier(0.9) == 1.0         # highsec
    assert security_multiplier(0.4) == 1.9         # lowsec, manufacturing
    assert security_multiplier(-0.1) == 2.1        # null, manufacturing
    assert security_multiplier(0.4, is_reaction=True) == 1.0
    assert security_multiplier(-0.1, is_reaction=True) == 1.1


# ── external-link allowlist ──────────────────────────────────────────────────
def test_external_host_allowed(app_module):
    f = app_module._external_host_allowed
    assert f("https://github.com/x")
    assert f("https://api.github.com/x")           # subdomain of allowed host
    assert f("https://esi.evetech.net/latest")
    assert f("https://ko-fi.com/retrovisor")
    assert not f("https://evil.example.com")
    assert not f("https://github.com.attacker.com")  # not a real subdomain
    assert not f("file:///etc/passwd")
    assert not f("javascript:alert(1)")


# ── best public-contract price (single vs bundle) ────────────────────────────
def _contracts_db():
    from app.web import contracts_helper
    conn = sqlite3.connect(":memory:")
    contracts_helper.ensure_public_contract_tables(conn)
    return conn, contracts_helper


def _add_contract(conn, cid, price, items):
    conn.execute(
        "INSERT INTO public_contracts (contract_id, region_id, type, price) "
        "VALUES (?,?, 'item_exchange', ?)", (cid, 10000002, price))
    for type_id, qty, incl in items:
        conn.execute(
            "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included) "
            "VALUES (?,?,?,?)", (cid, type_id, qty, incl))
    conn.commit()


def test_best_contract_price_prefers_single():
    conn, helper = _contracts_db()
    _add_contract(conn, 1, 100.0, [(34, 10, 1)])                 # single → 10/unit
    _add_contract(conn, 2, 50.0, [(34, 5, 1), (35, 1, 1)])       # bundle → 10/unit
    best = helper.best_contract_price(conn, 10000002, 34)
    assert best is not None
    assert best["is_bundle"] is False
    assert best["price"] == 10.0
    assert best["single_count"] == 1


def test_best_contract_price_bundle_fallback():
    conn, helper = _contracts_db()
    _add_contract(conn, 2, 50.0, [(34, 5, 1), (35, 1, 1)])       # only a bundle exists
    best = helper.best_contract_price(conn, 10000002, 34)
    assert best is not None
    assert best["is_bundle"] is True


def test_best_contract_price_none_when_absent():
    conn, helper = _contracts_db()
    _add_contract(conn, 1, 100.0, [(99, 10, 1)])                 # different product
    assert helper.best_contract_price(conn, 10000002, 34) is None
