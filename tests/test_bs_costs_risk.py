import math
from datetime import date

import pytest

from app import bs
from app.costs import round_trip
from app.instruments import OptionUniverse
from app.risk import RiskContext, check
from app.strategy import Quote, find_signal, make_plan, opening_range

from conftest import DAY, at, bars5, contracts


def test_put_call_parity_and_iv_roundtrip():
    S, K, T, r, v = 25600.0, 25650.0, 6 / 365, 0.06, 0.14
    c, p = bs.price(S, K, T, r, v, "CE"), bs.price(S, K, T, r, v, "PE")
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)
    assert bs.implied_vol(c, S, K, T, r, "CE") == pytest.approx(v, abs=1e-4)
    assert bs.implied_vol(p, S, K, T, r, "PE") == pytest.approx(v, abs=1e-4)
    assert 0 < bs.delta(S, K, T, r, v, "CE") < 1
    assert -1 < bs.delta(S, K, T, r, v, "PE") < 0
    assert bs.theta_per_day(S, K, T, r, v, "CE") < 0
    assert bs.implied_vol(0.01, S, 25000, T, r, "CE") is None  # below intrinsic


def test_round_trip_costs_by_hand(s):
    c = round_trip(100.0, 120.0, 65, "nse", s)
    assert c.brokerage == 40.0
    assert c.stt == pytest.approx(0.0015 * 7800)
    assert c.exchange == pytest.approx(0.0003503 * 14300)
    assert c.sebi == pytest.approx(10 / 1e7 * 14300)
    assert c.stamp == pytest.approx(0.00003 * 6500)
    assert c.gst == pytest.approx(0.18 * (40 + 0.0003503 * 14300 + 10 / 1e7 * 14300))
    assert c.total == pytest.approx(c.brokerage + c.stt + c.exchange + c.sebi + c.stamp + c.gst)
    b = round_trip(100.0, 120.0, 20, "bse", s)
    assert b.exchange == pytest.approx(0.000325 * 4400)
    s.brokerage_per_order = 50  # e.g. a different NRI rate
    assert round_trip(100.0, 120.0, 65, "nse", s).brokerage == 100


RANGE = [(25600, 25640, 25590, 25630), (25630, 25650, 25610, 25620), (25620, 25635, 25580, 25600),
         (25650, 25680, 25645, 25672)]


def _plan(s, quote=None):
    if s.max_loss_per_trade == 2000:
        s.max_loss_per_trade = 3000  # one lot of this plan risks about Rs 2,050
    u = OptionUniverse("NIFTY", contracts())
    bars = bars5(DAY, RANGE)
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    c = u.contract(date(2026, 11, 17), 25650, "CE")
    T = bs.years_to_expiry(at(DAY, "09:35"), c.expiry)
    px = round(bs.price(25672, 25650, T, 0.06, 0.13, "CE"), 1)
    q = quote or Quote(ltp=px, bid=px - 0.3, ask=px + 0.3)
    return make_plan(sig, orng, c, q, 25672, at(DAY, "09:35", 5), s, "nse", []), q


def _ctx(q, **kw):
    base = dict(now=at(DAY, "09:36"), feed_live=True, feed_reason="", market_open=True, trades_today=0,
                realized_today=0.0, open_risk=0.0, spot_now=25672.0, quote=q, index_expiry_today=False)
    base.update(kw)
    return RiskContext(**base)


def _blocked(res):
    return {c.name for c in res.checks if c.status == "BLOCK"}


def test_clean_signal_passes(s):
    p, q = _plan(s)
    res = check(p, _ctx(q), s)
    assert res.verdict == "PASS", res.to_json()


@pytest.mark.parametrize("kw,name", [
    ({"feed_live": False, "feed_reason": "no prices for 40 s"}, "Data"),
    ({"trades_today": 3}, "Trades today"),
    ({"realized_today": -5000.0}, "Daily loss"),
    ({"now": at(DAY, "14:01")}, "Time window"),
    ({"market_open": False}, "Time window"),
    ({"now": at(DAY, "09:47")}, "Signal age"),
    ({"spot_now": 25610.0}, "Price vs plan"),
    ({"spot_now": 25800.0}, "Price vs plan"),
])
def test_each_rule_blocks(s, kw, name):
    p, q = _plan(s)
    res = check(p, _ctx(q, **kw), s)
    assert res.verdict == "BLOCK" and name in _blocked(res)
    assert any(c.message for c in res.checks if c.name == name)


def test_wide_spread_and_premium_bounds_block(s):
    p, q = _plan(s)
    wide = Quote(ltp=q.ltp, bid=q.ltp - 5, ask=q.ltp + 5)
    assert "Spread" in _blocked(check(p, _ctx(wide), s))
    s.nifty_max_premium = 50
    assert "Premium" in _blocked(check(p, _ctx(q), s))


def test_too_big_for_max_loss_blocks(s):
    s.max_loss_per_trade = 300
    p, q = _plan(s)
    res = check(p, _ctx(q), s)
    assert p.sizing.lots == 0 and "Max loss" in _blocked(res)


def test_unconfirmed_limits_block_live_but_warn_in_demo(s):
    s.max_loss_confirmed = False
    p, q = _plan(s)
    assert "Your limits" in _blocked(check(p, _ctx(q), s))
    res = check(p, _ctx(q, demo=True), s)
    assert res.verdict == "PASS" and any(c.name == "Your limits" and c.status == "WARN" for c in res.checks)


def test_expiry_day_warns_and_same_day_contract_blocks(s):
    p, q = _plan(s)
    res = check(p, _ctx(q, index_expiry_today=True), s)
    assert any(c.name == "Expiry" and c.status == "WARN" for c in res.checks)
    res = check(p, _ctx(q, now=at(date(2026, 11, 17), "09:36"), market_open=True), s)
    assert "Expiry" in _blocked(res)
