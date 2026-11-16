import pytest
from datetime import datetime

from app.paper import CAPITAL, PaperError
from app.timeutil import IST, hm, ist
from test_engine_server import make_demo, run_until


def _demo(tmp_path, **kw):
    """a demo day with the 09:15 auto straddle switched off (these tests are about single trades)"""
    s, e, src, j = make_demo(tmp_path, **kw)
    e.paper.set_auto_straddle(False)
    return s, e, src, j


def _split_adds_up(t):
    s = t["split"]
    total = s["delta"] + s["theta"] + s["vega"] + s["spread"] + s["charges"]
    assert total == pytest.approx(t["pnl"], abs=1.0)


def test_one_click_index_trade_follows_the_tested_signal(tmp_path):
    s, e, src, j = _demo(tmp_path, exit_mode="trail")
    desk = e.paper
    run_until(src, "09:20")
    tk = desk.ticket("index", "NIFTY", src.clock.t)
    assert tk["system_side"] is None and tk["account"]["capital"] == CAPITAL
    run_until(src, "10:00")                                  # the demo day's NIFTY breakout signal is at 09:50
    tk = desk.ticket("index", "NIFTY", src.clock.t)
    side = tk["system_side"]
    assert side == e.idx["NIFTY"].plan["side"]
    sd = tk["sides"][side]
    assert sd["tested"] and sd["can_buy"] and sd["qty"] == 65 and sd["price"] > 0 and "Tested" in sd["rule"]
    assert sd["contract"].startswith("NIFTY") and sd["token"] == e.idx["NIFTY"].plan["contract"]["token"]
    t = desk.open("index", "NIFTY", side, src.clock.t)
    assert t["status"] == "open" and t["rule"] == "tested-orb"
    acct = desk.account(src.clock.t)
    assert acct["cash"] == pytest.approx(CAPITAL - t["entry_premium"] * 65, abs=0.01) and acct["open"] == 1
    with pytest.raises(PaperError):                           # one open paper trade per symbol
        desk.open("index", "NIFTY", side, src.clock.t)
    run_until(src, "11:00")
    closed = desk.exit(t["id"], src.clock.t)
    assert closed["status"] == "closed" and closed["exit_reason"] == "you exited"
    _split_adds_up(closed)
    acct = desk.account(src.clock.t)
    assert acct["trades"] == 1 and acct["balance"] == pytest.approx(CAPITAL + closed["pnl"], abs=0.01)


def test_own_trade_exits_by_itself_and_cash_is_checked(tmp_path):
    s, e, src, j = _demo(tmp_path)
    desk = e.paper
    run_until(src, "10:05")
    tk = desk.ticket("index", "SENSEX", src.clock.t)
    side = "CE" if tk["system_side"] != "CE" else "PE"         # the side the system did NOT pick: your own call
    sd = tk["sides"][side]
    assert not sd["tested"] and sd["stop"] and sd["target"] and "Your call" in sd["rule"]
    t = desk.open("index", "SENSEX", side, src.clock.t)
    assert t["rule"] == "manual"
    run_until(src, "15:20")                                   # stop, target or the 15:15 square-off
    done = j.paper_get(t["id"])
    assert done["status"] == "closed" and done["exit_reason"] in ("stop hit", "target hit", "time exit 15:15")
    tr = desk.trades(src.clock.t)
    _split_adds_up(tr["closed"][0])
    assert tr["account"]["theta"] != 0
    tk = desk.ticket("index", "NIFTY", src.clock.t)
    assert not tk["sides"]["CE"]["can_buy"] and "15:15" in tk["sides"]["CE"]["why"]
    j.paper_reset(e.mode, 1000.0)                               # tiny account: not enough cash
    src.control("restart"); e.evaluate()
    run_until(src, "10:05")
    tk = desk.ticket("index", "NIFTY", src.clock.t)
    assert not tk["sides"]["CE"]["can_buy"] and "cash" in tk["sides"]["CE"]["why"]


def test_stock_paper_trade(tmp_path):
    s, e, src, j = _demo(tmp_path)
    desk = e.paper
    run_until(src, "10:30")
    name = next(iter(e.movers.stocks))
    tk = desk.ticket("stock", name, src.clock.t)
    assert tk["system_side"] is None and "your own call" in tk["system_text"]
    sd = tk["sides"]["CE"]
    assert sd["can_buy"] and sd["qty"] == e.movers.stocks[name].info.lot
    t = desk.open("stock", name, "CE", src.clock.t)
    assert t["kind"] == "stock" and t["token"] if "token" in t else True
    assert e.paper.tokens                                        # the stock option is subscribed for quotes
    run_until(src, "15:20")
    tr = desk.trades(src.clock.t)
    assert tr["open"] == [] and tr["closed"][0]["exit_reason"] in ("stop hit", "target hit", "time exit 15:15")
    _split_adds_up(tr["closed"][0])


def test_listed_stock_breakout_is_one_click(tmp_path):
    """A stock on the dashboard's lists: the ticket marks its side as the signal and the paper trade follows the list's
    rule (stop, 2R target, back inside the range, 15:00) with the listed contract."""
    from app.predict import Predictor
    s, e, src, j = _demo(tmp_path)
    pr = Predictor(e)
    row = None
    while src.clock.t < ist(src.day, hm("13:30")):
        src.advance(300)
        snap = pr.snapshot()
        lists = snap["stock_trades"]
        assert len(lists["picks"]["live"]) <= 3 and len(lists["movers"]["live"]) <= 5
        for k in ("picks", "movers"):
            for r in lists[k]["live"]:
                assert r["contract"] and r["stop"] and r["target"] and r["token"]       # every row is a full trade
        live = lists["picks"]["live"] + lists["movers"]["live"]
        if live:
            row = live[0]
            break
    assert row, "the demo day should give at least one listed stock breakout"
    tk = e.paper.ticket("stock", row["name"], src.clock.t)
    assert tk["system_side"] == row["side"] and "System: BUY" in tk["system_text"] and "paper only" in tk["system_text"]
    sd = tk["sides"][row["side"]]
    assert sd["signal"] == "listed" and sd["token"] == row["token"]
    assert sd["stop"] == row["stop"] and sd["target"] == row["target"]
    t = e.paper.open("stock", row["name"], row["side"], src.clock.t)
    assert t["rule"] == "listed"
    while src.clock.t < ist(src.day, hm("15:05")) and j.paper_get(t["id"])["status"] == "open":
        src.advance(60)
        if src.clock.t.minute % 5 == 0:
            pr.snapshot()
    done = j.paper_get(t["id"])
    assert done["status"] == "closed"
    assert done["exit_reason"] in ("stop hit", "target hit", "back inside the range", "time exit 15:00")


def test_listed_trade_keeps_running_back_inside_the_range(tmp_path):
    """A listed stock trade exits only at its stop, its 2R target or 15:00 - not when price slips back inside."""
    from datetime import timedelta
    from app.predict import Predictor
    s, e, src, j = _demo(tmp_path)
    pr = Predictor(e)
    row = None
    while src.clock.t < ist(src.day, hm("13:30")) and row is None:
        src.advance(300)
        L = pr.snapshot()["stock_trades"]
        live = L["picks"]["live"] + L["movers"]["live"]
        row = live[0] if live else None
    assert row
    t = e.paper.open("stock", row["name"], row["side"], src.clock.t)
    assert "back inside" not in t["rule_text"] and "15:00" in t["rule_text"]
    while src.clock.t < ist(src.day, hm("15:05")) and j.paper_get(t["id"])["status"] == "open":
        src.advance(60)
    done = j.paper_get(t["id"])
    assert done["exit_reason"] in ("stop hit", "target hit", "time exit 15:00")
    sd = 1 if done["side"] == "CE" else -1
    if done["exit_reason"] == "stop hit":
        assert (done["exit_spot"] - done["stop_spot"]) * sd <= 0      # really at or beyond the stop


def test_restarted_replay_does_not_settle_trades_from_an_earlier_run(tmp_path):
    s, e, src, j = _demo(tmp_path)
    run_until(src, "10:05")
    tk = e.paper.ticket("index", "NIFTY", src.clock.t)
    t_open = e.paper.open("index", "NIFTY", "CE", src.clock.t)
    t_done = e.paper.open("index", "SENSEX", "PE", src.clock.t)
    run_until(src, "10:20")
    e.paper.exit(t_done["id"], src.clock.t)
    src.control("restart"); e.evaluate()                       # back to 09:15: the open trade never happened
    assert j.paper_get(t_open["id"]) is None
    assert j.paper_get(t_done["id"])["status"] == "closed"     # closed trades stay as history
    # the app restarted (not the Restart button): an open trade "from the future" is removed, not stopped out at 09:15
    run_until(src, "10:30")
    t2 = e.paper.open("index", "NIFTY", "PE", src.clock.t)
    src.clock.t = ist(src.day, hm("09:20"))
    e.paper.step(src.clock.t, e.feed_status(src.clock.t))
    assert j.paper_get(t2["id"]) is None
    assert all(not (x["closed_at"] and x["closed_at"] < x["opened_at"]) for x in j.paper_list(e.mode))


def test_dashboard_warns_when_a_newer_version_is_installed(tmp_path, monkeypatch):
    from app import engine as E
    s, e, src, j = _demo(tmp_path)
    assert e.state_json()["build_on_disk"] == E._build()          # same files as running: no warning
    monkeypatch.setitem(E._DISK, "build", "99.9")
    monkeypatch.setitem(E._DISK, "at", 1e18)
    assert e.state_json()["build_on_disk"] == "99.9"              # newer files on disk -> the page shows a banner


def test_one_click_straddle_and_exit_both(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    e.paper.set_auto_straddle(False)
    run_until(src, "10:00")
    tk = e.paper.ticket("index", "NIFTY", src.clock.t)
    q = tk["straddle"]
    assert q["can_buy"] and q["cost"] == pytest.approx((q["price_ce"] + q["price_pe"]) * q["qty"], abs=0.5)
    assert q["breakeven_dn"] < q["strike"] < q["breakeven_up"]
    legs = e.paper.open_straddle("index", "NIFTY", src.clock.t)
    assert [x["side"] for x in legs] == ["CE", "PE"] and legs[0]["strike"] == legs[1]["strike"]
    assert legs[0]["group"] == legs[1]["group"] and legs[0]["rule"] == "straddle"
    tk = e.paper.ticket("index", "NIFTY", src.clock.t)
    assert len(tk["open_list"]) == 2 and not tk["straddle"]["can_buy"]          # one position per symbol
    run_until(src, "11:00")
    out = e.paper.exit_group(legs[0]["group"], src.clock.t)
    assert all(x["status"] == "closed" and x["exit_reason"] == "you exited" for x in out)
    # a straddle left alone closes at 15:15, not at any stop
    legs = e.paper.open_straddle("index", "SENSEX", src.clock.t)
    run_until(src, "15:20")
    done = [j.paper_get(x["id"]) for x in legs]
    assert all(t["status"] == "closed" and t["exit_reason"] == "time exit 15:15" for t in done)


def test_auto_straddle_at_0915_on_three_indices(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    run_until(src, "09:20")
    auto = [t for t in j.paper_list(e.mode) if t["rule"] == "straddle"]
    names = {t["name"] for t in auto}
    assert names == {"NIFTY", "BANKNIFTY", "SENSEX"} and len(auto) == 6
    for t in auto:
        opened = datetime.fromisoformat(t["opened_at"]).astimezone(IST)
        assert opened.hour == 9 and opened.minute == 15                     # bought at the first price after 09:15
    run_until(src, "10:00")
    assert len([t for t in j.paper_list(e.mode) if t["rule"] == "straddle"]) == 6   # once a day
    src.control("restart"); e.evaluate()                                   # a restarted replay buys them again
    run_until(src, "09:20")
    assert len([t for t in j.paper_list(e.mode) if t["rule"] == "straddle"]) == 6
    e.paper.set_auto_straddle(False)
    src.control("restart"); e.evaluate()
    run_until(src, "09:25")
    assert not [t for t in j.paper_list(e.mode) if t["rule"] == "straddle"]


def test_straddle_and_single_trade_can_be_open_together(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    run_until(src, "10:00")                       # the 09:15 auto straddle is open on NIFTY
    tk = e.paper.ticket("index", "NIFTY", src.clock.t)
    assert len(tk["straddle_open"]) == 2 and tk["open"] is None
    assert tk["sides"]["CE"]["can_buy"] and not tk["straddle"]["can_buy"]
    t = e.paper.open("index", "NIFTY", "CE", src.clock.t)
    tk = e.paper.ticket("index", "NIFTY", src.clock.t)
    assert tk["open"]["id"] == t["id"] and len(tk["straddle_open"]) == 2
    assert not tk["sides"]["PE"]["can_buy"]        # still one single trade per symbol


def test_more_than_one_lot(tmp_path):
    s, e, src, j = _demo(tmp_path)
    run_until(src, "10:05")
    t1 = e.paper.ticket("index", "NIFTY", src.clock.t, lots=1)
    t3 = e.paper.ticket("index", "NIFTY", src.clock.t, lots=3)
    assert t3["lots"] == 3 and t3["sides"]["PE"]["qty"] == 3 * 65
    assert t3["sides"]["PE"]["cost"] == pytest.approx(3 * t1["sides"]["PE"]["cost"], rel=1e-6)
    assert t3["sides"]["PE"]["max_loss"] > 2.5 * t1["sides"]["PE"]["max_loss"]
    assert t3["straddle"]["qty"] == 3 * 65 and t3["straddle"]["cost"] == pytest.approx(3 * t1["straddle"]["cost"], rel=1e-6)
    t = e.paper.open("index", "NIFTY", "PE", src.clock.t, lots=3)
    assert t["qty"] == 195
    run_until(src, "11:00")
    done = e.paper.exit(t["id"], src.clock.t)
    _split_adds_up(done)
    legs = e.paper.open_straddle("index", "SENSEX", src.clock.t, lots=2)
    assert all(x["qty"] == 40 for x in legs)
    assert e.paper.ticket("index", "NIFTY", src.clock.t, lots=99)["lots"] == 50     # capped
    j.paper_reset(e.mode, 50_000.0)
    assert not e.paper.ticket("index", "NIFTY", src.clock.t, lots=20)["sides"]["CE"]["can_buy"]   # cash check scales
