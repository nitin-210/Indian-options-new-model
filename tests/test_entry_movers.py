from datetime import date

import pytest

from app import bs
from app.instruments import OptionUniverse
from app.movers import FnoStock, Movers, buildup, parse_fno_stocks
from app.risk import RiskContext, check
from app.strategy import Quote, compare_strikes, find_signal, make_plan, opening_range

from conftest import DAY, at, bars5, contracts

RANGE = [(25600, 25640, 25590, 25630), (25630, 25650, 25610, 25620), (25620, 25635, 25580, 25600),
         (25650, 25680, 25645, 25672)]  # range 25580-25650, mid 25615; CE signal at 25672, risk 57


def _setup(s):
    s.max_loss_per_trade = 3000
    u = OptionUniverse("NIFTY", contracts())
    bars = bars5(DAY, RANGE)
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    exp = date(2026, 11, 17)
    now = at(DAY, "09:35", 5)
    T = bs.years_to_expiry(now, exp)
    quotes = {}
    for k in u.strikes(exp):
        for opt in ("CE", "PE"):
            c = u.contract(exp, k, opt)
            px = round(bs.price(25672, k, T, 0.06, 0.13, opt), 1)
            quotes[c.token] = Quote(ltp=px, bid=px - 0.2, ask=px + 0.2)
    return u, orng, sig, exp, now, quotes


def test_entry_zone_levels(s):
    u, orng, sig, exp, now, quotes = _setup(s)
    c = u.contract(exp, 25650, "CE")
    p = make_plan(sig, orng, c, quotes[c.token], 25672, now, s, "nse", [])
    # at 2R and a 1:1 minimum, the furthest entry is halfway between stop and target
    assert p.max_entry_index == pytest.approx((25615 + 25786) / 2, abs=0.05)
    assert p.max_entry_premium > p.entry_fill
    assert p.entry_limit == pytest.approx(p.entry_fill, abs=0.05)
    s.min_reward_risk = 2.0  # stricter: no room at all above the signal close
    p2 = make_plan(sig, orng, c, quotes[c.token], 25672, now, s, "nse", [])
    assert p2.max_entry_index == pytest.approx(25672, abs=0.05)


def _ctx(q, spot):
    return RiskContext(now=at(DAY, "09:36"), feed_live=True, feed_reason="", market_open=True, trades_today=0,
                       realized_today=0.0, open_risk=0.0, spot_now=spot, quote=q, index_expiry_today=False)


def test_chasing_is_blocked(s):
    u, orng, sig, exp, now, quotes = _setup(s)
    c = u.contract(exp, 25650, "CE")
    p = make_plan(sig, orng, c, quotes[c.token], 25672, now, s, "nse", [])
    ok = check(p, _ctx(quotes[c.token], 25690), s)
    assert any(x.name == "Entry zone" and x.status == "PASS" for x in ok.checks)
    late = check(p, _ctx(quotes[c.token], 25710), s)
    assert late.verdict == "BLOCK" and any(x.name == "Entry zone" and x.status == "BLOCK" for x in late.checks)


def test_strike_comparison_picks_a_sensible_best(s):
    u, orng, sig, exp, now, quotes = _setup(s)
    rows, best = compare_strikes(sig, orng, u, exp, quotes, 25672, now, s, "nse", [])
    assert [r["label"] for r in rows] == ["2 ITM", "1 ITM", "ATM", "1 OTM", "2 OTM"]
    assert [r["strike"] for r in rows] == [25550, 25600, 25650, 25700, 25750]  # calls: ITM = lower strikes
    assert best is not None and best["best"] and best["ok"]
    assert 0.40 <= abs(best["delta"]) <= 0.70
    eligible = [r for r in rows if r["ok"]]
    top = max(r["rr"] for r in eligible)
    assert best["rr"] >= top - 0.05  # within the tie band
    assert "after charges and time decay" in best["reason"].lower()
    assert sum(1 for r in rows if r["best"]) == 1


def test_comparison_flags_what_fails(s):
    u, orng, sig, exp, now, quotes = _setup(s)
    s.max_loss_per_trade = 1500  # deep ITM lots no longer fit
    s.max_spread_pct = 0.1  # and every live spread is too wide
    rows, best = compare_strikes(sig, orng, u, exp, quotes, 25672, now, s, "nse", [])
    assert best is None
    assert all(not r["ok"] for r in rows)
    assert any("spread" in " ".join(r["why_not"]) for r in rows)


def test_put_comparison_uses_higher_strikes_for_itm(s):
    s.max_loss_per_trade = 3000
    u = OptionUniverse("NIFTY", contracts())
    bars = bars5(DAY, RANGE[:3] + [(25600, 25610, 25560, 25570)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    rows, _ = compare_strikes(sig, orng, u, date(2026, 11, 17), {}, 25570, at(DAY, "09:35", 5), s, "nse", [])
    assert [r["strike"] for r in rows] == [25650, 25600, 25550, 25500, 25450]
    assert all("no live quote" in r["why_not"] for r in rows)


def test_buildup_labels():
    assert buildup(1.2, 5000) == ("Long build-up", "bull")
    assert buildup(1.2, -5000) == ("Short covering", "bull")
    assert buildup(-1.2, 5000) == ("Short build-up", "bear")
    assert buildup(-1.2, -5000) == ("Long unwinding", "bear")
    assert buildup(1.0, None) == ("—", "")


def test_movers_rank_and_select():
    stocks = [FnoStock(n, f"E{i}", f"F{i}", f"{n}FUT", date(2026, 11, 24), 500) for i, n in enumerate("ABCDE")]
    from app.instruments import Contract
    opts = [Contract(f"O{k}{o}", f"A{k}{o}", "A", date(2026, 11, 24), float(k), o, 500, 0.05, "NFO")
            for k in (90, 100, 110) for o in ("CE", "PE")]
    m = Movers()
    m.load(stocks, opts, "test")
    assert m.tokens() == {1: {"E0", "E1", "E2", "E3", "E4"}, 2: {"F0", "F1", "F2", "F3", "F4"}}
    now = at(DAY, "10:00")
    for i, pct in enumerate([3.0, -2.0, 1.0, -4.0, 0.5]):
        assert m.on_tick({"ws_type": 1, "token": f"E{i}", "ltp": 100 * (1 + pct / 100), "close": 100, "vol": 10000}, now, {})
        m.on_tick({"ws_type": 2, "token": f"F{i}", "ltp": 101 * (1 + pct / 100), "close": 101, "oi": 1100}, now, {f"F{i}": 1000})
    snap = m.snapshot(3, None)
    assert [r["name"] for r in snap["bull"]] == ["A", "C", "E"]
    assert [r["name"] for r in snap["bear"]] == ["D", "B"]
    assert snap["bull"][0]["buildup"] == "Long build-up" and snap["bear"][0]["buildup"] == "Short build-up"
    assert not m.on_tick({"ws_type": 2, "token": "nope", "ltp": 1}, now, {})
    toks = m.select("A", DAY)
    assert m.atm_tokens == {"O100CE", "O100PE"} and m.selected_atm == 100.0  # ATM of a stock at 103
    assert toks == {f"O{k}{o}" for k in (90, 100, 110) for o in ("CE", "PE")}  # ATM +-2 (only 3 strikes exist)


def test_parse_fno_stocks_rows():
    rows = [
        {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "exch_seg": "NSE", "instrumenttype": ""},
        {"token": "5001", "symbol": "RELIANCE24NOV26FUT", "name": "RELIANCE", "expiry": "24NOV2026", "strike": "-1",
         "lotsize": "500", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
        {"token": "5002", "symbol": "RELIANCE29DEC26FUT", "name": "RELIANCE", "expiry": "29DEC2026", "strike": "-1",
         "lotsize": "500", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
        {"token": "6001", "symbol": "RELIANCE24NOV261420CE", "name": "RELIANCE", "expiry": "24NOV2026",
         "strike": "142000.000000", "lotsize": "500", "instrumenttype": "OPTSTK", "exch_seg": "NFO", "tick_size": "5"},
        {"token": "7001", "symbol": "NOEQ24NOV26FUT", "name": "NOEQ", "expiry": "24NOV2026", "strike": "-1",
         "lotsize": "100", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
    ]
    stocks, opts = parse_fno_stocks(rows, date(2026, 11, 11))
    assert len(stocks) == 1
    st = stocks[0]
    assert (st.name, st.eq_token, st.fut_token, st.lot) == ("RELIANCE", "2885", "5001", 500)
    assert len(opts) == 1 and opts[0].strike == 1420 and opts[0].opt == "CE"


def test_best_mode_uses_the_best_contract(tmp_path):
    from test_engine_server import make_demo, run_until
    s, e, src, j = make_demo(tmp_path, history=False)
    e.s.strike_mode = "best"
    run_until(src, "09:52")
    p = e.idx["NIFTY"].plan
    assert p["best"] and p["contract"]["token"] == p["best"]["token"]
    mj = e.movers_json()
    assert mj["bull"] and mj["bear"] and mj["tracked"] == 25
    assert e.select_mover(mj["bull"][0]["name"])
    src.advance(15)
    d = e.movers_json()["detail"]
    assert d["name"] == mj["bull"][0]["name"] and len(d["options"]) == 2
    assert all(o["ltp"] for o in d["options"])


def test_signal_found_after_the_fact_is_missed_not_planned(tmp_path):
    """Starting the app late must not build a plan from later prices or log a trade."""
    from datetime import timedelta
    from app import demo as demomod
    from app.candles import Bar
    from app.config import Settings
    from app.engine import Engine, Hub
    from app.journal import Journal
    from app.sources import DemoSource, SimClock
    from app.timeutil import Calendar, hm, ist

    day = demomod.DEMO_DAY
    clock = SimClock(ist(day, hm("11:00")), 10)
    j = Journal(tmp_path / "j.db")
    e = Engine(Settings(), Calendar(), j, "live", Hub(), clock)
    src = DemoSource(e, e.s, clock)
    e.set_contracts(src._contracts)
    e.evaluate()
    path = src.paths["NIFTY"]
    bars = []
    for m in range(0, 105):  # 09:15 .. 10:59 one-minute bars from the demo path
        seg = path[m * 60:(m + 1) * 60 + 1]
        bars.append(Bar(ist(day, hm("09:15")) + timedelta(minutes=m), float(seg[0]), float(seg.max()),
                        float(seg.min()), float(seg[-1]), 0.0, True, 1))
    e.backfill("NIFTY", bars)
    e.evaluate()
    st = e.idx["NIFTY"]
    assert st.phase == "missed" and st.plan is None and st.trade_id is None
    assert j.list("live") == []
    m = st.missed
    assert m["side"] == "CE" and m["signal_time"].endswith("09:50:00+05:30")
    assert m["outcome"] in ("open", "target", "stop")
    card = e.state_json()["indices"]["NIFTY"]["card"]
    assert card["state"] == "missed" and "while the app was off" in card["title"]


def test_backtest_report_runs_and_reads(s):
    from datetime import date
    from app import demo as demomod
    from app.backtest import format_report, run, write_csv
    bars = demomod.synthetic_history("NIFTY", date(2026, 11, 6), 60, 5)
    vix = demomod.synthetic_vix(date(2026, 11, 6), 60, 5)
    res = run("NIFTY", bars, vix, s, 65, 50.0, "nse", date(2026, 11, 6))
    o = res["overall"]
    assert o["n"] == res["summary"]["fired"] > 0
    assert abs(o["target_pct"] + o["stop_pct"] + o["neither_pct"] - 100) < 0.01
    assert len(res["variants"]) == 9
    text = format_report([res], s)
    assert "target first" in text and "ESTIMATE" in text and "<- your setting" in text
    assert [v["label"] for v in res["variants"] if v["current"]] == ["original: any close, 2R target"]
    # the default (revised) rule
    from app.config import Settings
    d = Settings()
    res2 = run("NIFTY", bars, vix, d, 65, 50.0, "nse", date(2026, 11, 6))
    text2 = format_report([res2], d)
    assert "stopped out" in text2 and "Original rule vs A and B" in text2 and "no fixed target" in text2
    assert [v["label"] for v in res2["variants"] if v["current"]] == ["A with trailing exit"]
    assert res2["overall"]["target_pct"] == 0
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "t.csv"
    write_csv([res], p)
    assert p.read_text().count("\n") == o["n"] + 1


def test_old_stale_blocked_rows_are_cleaned(tmp_path):
    from datetime import date
    from app.journal import Journal
    j = Journal(tmp_path / "j.db")
    plan = {"index": "SENSEX", "side": "CE", "contract": {"symbol": "X", "token": "1", "lot": 20},
            "signal_time": "2026-11-06T09:35:00+05:30", "entry_index": 1, "stop_index": 0, "target_index": 2,
            "lots": 0, "entry_fill": 1731.0, "loss_at_stop": 0, "one_lot_loss": 24220.0}
    j.open_trade(day=date(2026, 11, 6), plan=plan, verdict="BLOCK",
                 reasons=["Signal is 496 min old (limit 10). A late entry is a different trade."], mode="live")
    j.open_trade(day=date(2026, 11, 6), plan=plan, verdict="BLOCK", reasons=["Spread too wide"], mode="live")
    j.close()
    j2 = Journal(tmp_path / "j.db")
    rows = j2.list("live")
    assert len(rows) == 1 and rows[0]["reasons"] == ["Spread too wide"]


def test_stock_strike_comparison(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_engine_server import make_demo, run_until
    s, e, src, j = make_demo(tmp_path, history=False)
    run_until(src, "09:25")
    name = e.movers_json()["bull"][0]["name"]
    e.select_mover(name)
    assert "opening range forms" in e.stock_compare(name)["error"]
    run_until(src, "10:40")
    e.select_mover(name)
    run_until(src, "10:45")
    d = e.movers_json()["detail"]
    c = d["compare"]
    assert c["side"] == "CE" and len(c["rows"]) == 5 and c["stop"] < d["ltp"]
    assert [r["label"] for r in c["rows"]] == ["2 ITM", "1 ITM", "ATM", "1 OTM", "2 OTM"]
    assert len(d["options"]) == 2                               # the ATM call and put cards
    pick = c["best"] or c["fallback"]
    assert pick and 0.40 <= abs(pick["delta"]) <= 0.70 and "₹" in pick["reason"]
    if c["best"] is None:                                       # demo lots are big: over a Rs 2,000 max loss
        assert pick["why_not"] == ["1 lot is over your max loss"]
    s.max_loss_per_trade = 1e6                                  # with room for a lot, a best fit is chosen
    c2 = e.stock_compare(name)
    assert c2["best"] and c2["best"]["ok"] and c2["fallback"] is None
    assert e.stock_compare("NOPE")["error"]
