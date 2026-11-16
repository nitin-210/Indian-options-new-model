import pytest
from datetime import date, timedelta

from app import odds
from app.candles import Bar
from app import demo as demomod
from app.demo import _weekly
from app.timeutil import hm, ist


def _day(d, o, c, rng, start="09:15", n=74):
    """n five-minute bars from `start`, drifting from o to c, with the given high-low range."""
    out, t0 = [], ist(d, hm(start))
    for i in range(n):
        p0 = o + (c - o) * i / n
        p1 = o + (c - o) * (i + 1) / n
        out.append(Bar(t0 + timedelta(minutes=5 * i), p0, max(p0, p1), min(p0, p1), p1, 0, True, 5))
    out[0].h = max(o, c) + rng / 2
    out[0].l = min(o, c) - rng / 2
    return out


def test_muhurat_session_is_not_yesterday():
    bars = []
    d = date(2026, 10, 15)
    while len({b.start.date() for b in bars}) < 15:
        if d.weekday() < 5:
            bars += _day(d, 100, 101, 2)
        d += timedelta(days=1)
    bars += _day(date(2026, 11, 6), 100, 110, 12)               # Friday: a wide up day (6x normal)
    bars += _day(date(2026, 11, 8), 110, 109, 0.5, "14:20", 12)  # Sunday Muhurat hour: small and down
    today = date(2026, 11, 9)
    y = odds.yesterday_for(bars, today)
    assert y["date"] == date(2026, 11, 6) and y["side"] == "CE"
    assert odds.yesterday_range_atr(bars, today) > 1.2          # so the tested rule sits today out
    assert date(2026, 11, 8) not in odds.yesterday_sides(bars + _day(today, 109, 108, 1)).keys() | \
        {d for d in odds.day_open_close(bars)}


def test_expiries_move_off_holidays():
    hol = {date(2026, 11, 10), date(2026, 11, 24)}
    assert _weekly("NIFTY", date(2026, 11, 9), 3, hol) == [date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 23)]
    assert _weekly("BANKNIFTY", date(2026, 11, 9), 2, hol) == [date(2026, 11, 23), date(2026, 12, 29)]
    assert _weekly("SENSEX", date(2026, 11, 9), 2, hol) == [date(2026, 11, 12), date(2026, 11, 19)]


def test_replay_restart_keeps_the_wide_day_skip(tmp_path):
    from test_engine_server import make_demo
    s, e, src, j = make_demo(tmp_path)
    st = e.idx["NIFTY"]
    assert st.yday and "range_atr" in st.yday
    src.control("restart")
    e.evaluate()
    assert st.yday and "range_atr" in st.yday


def test_stock_chart_never_mixes_two_sessions(tmp_path):
    from test_engine_server import make_demo
    from app.feed import MODE_SNAP
    s, e, src, j = make_demo(tmp_path)
    info = src._stocks[0][0]
    day = src.day
    prev = e.cal.prev_trading_day(day)
    t0 = ist(prev, hm("09:15"))
    old = [Bar(t0 + timedelta(minutes=m), 500 + m, 501 + m, 499 + m, 500.5 + m, 10, True, 1) for m in range(375)]
    e.backfill_stock(info.name, old, prev)                      # opened before today's first trade
    assert e.stock_json(info.name, 5)["day"] == prev.isoformat()
    now = ist(day, hm("09:15")) + timedelta(seconds=5)
    src.clock.t = now
    e.on_tick({"mode": MODE_SNAP, "ws_type": 1, "token": info.eq_token, "ts": int(now.timestamp() * 1000),
               "ltp": 820.0, "close": 800.0, "open": 815.0, "vol": 100}, now.timestamp())
    js = e.stock_json(info.name, 1)
    assert js["day"] == day.isoformat() and js["today"]
    assert [b["time"] for b in js["bars"]] and all(abs(b["open"] - 820) < 1 for b in js["bars"])
    assert js["or"]["high"] == 820.0 and js["or"]["low"] == 820.0


def test_trade_left_open_is_settled_from_candles(tmp_path):
    import json
    from test_engine_server import make_demo, run_until
    s, e, src, j = make_demo(tmp_path)
    run_until(src, "10:00")
    st = e.idx["NIFTY"]
    assert st.trade_id
    tr = j.get(st.trade_id)
    assert tr["status"] == "open"
    p = json.loads(tr["plan_json"])
    t_in = __import__("datetime").datetime.fromisoformat(p["signal_time"])
    ce = p["side"] == "CE"
    # the index then runs 1R in favour (version B sells half), comes back and hits the stop at 11:00
    risk = abs(p["entry_index"] - p["stop_index"])
    idx, opt = [], []
    for m in range(0, 330):
        t = t_in + timedelta(minutes=m)
        if m < 20:
            px = p["entry_index"] + (1 if ce else -1) * risk * 1.1 * m / 19
        else:
            px = p["entry_index"] + (1 if ce else -1) * risk * (1.1 - 2.3 * (m - 19) / 40)
        idx.append(Bar(t, px, px + 1, px - 1, px, 0, True, 1))
        opt.append(Bar(t, 100, 100, 100, 100 + (px - p["entry_index"]) * (0.5 if ce else -0.5), 0, True, 1))
    closed = e.settle_from_candles(tr, idx, opt)
    assert closed and closed["status"] == "closed" and "app was off" in closed["exit_reason"]
    v = json.loads(j.get(st.trade_id)["versions_json"])
    assert v["B"]["half"] is not None and v["A"]["exit"]["reason"] == "stop"
    assert v["B"]["exit"]["reason"] == "stop at entry"
    assert st.trade_id not in [t["id"] for t in j.open_before(src.day + timedelta(days=1), e.mode)]


def test_range_breaks_list_matches_the_picks(tmp_path):
    from test_engine_server import make_demo, run_until
    from app.predict import Predictor
    s, e, src, j = make_demo(tmp_path)
    e.stock_daily.update(__import__("app.demo", fromlist=["x"]).demo_stock_daily(src._stocks, src.day))
    pr = Predictor(e)
    run_until(src, "09:29")
    assert pr.snapshot()["breakouts"] == []          # nothing before the range is complete
    run_until(src, "12:00")
    snap = pr.snapshot()
    brks = snap["breakouts"]
    assert brks, "the demo day has stocks that break their range"
    times = [b["time"] for b in brks]
    assert times == sorted(times, reverse=True)
    for b in brks:
        assert b["side"] in ("CE", "PE") and b["status"] in ("holding", "back inside", "reversed")
        assert (b["close"] > b["trigger"]) if b["side"] == "CE" else (b["close"] < b["trigger"])
    by = {b["name"]: b for b in brks}
    for p in snap["picks"]:
        if p.get("breakout"):
            assert by[p["name"]]["side"] == p["breakout"]["side"] and by[p["name"]]["time"] == p["breakout"]["time"]
            assert by[p["name"]]["in_picks"]
    top = [b for b in brks if b["top_mover"]]
    assert top, "the demo day has top movers that break their range"
    for b in brks:
        assert b["top_mover"] == (b["mover_rank"] is not None and b["mover_rank"] <= 10 and not b["late"])
        if b["mover_rank"] is not None:
            assert (b["mover_pct"] > 0) if b["side"] == "CE" else (b["mover_pct"] < 0)
    # a replay restart starts the list again
    src.control("restart")
    e.evaluate()
    run_until(src, "09:40")
    assert all(b["time"] <= "09:40" for b in pr.snapshot()["breakouts"])


def test_closing_auction_candles_stay_out_of_the_range():
    from datetime import date
    bars = []
    d = date(2026, 10, 15)
    while len({b.start.date() for b in bars}) < 15:
        if d.weekday() < 5:
            bars += _day(d, 100, 101, 2, n=72)                     # 09:15 .. 15:10
            t = ist(d, hm("15:20"))
            bars.append(Bar(t, 101, 140, 60, 101, 0, True, 5))     # an auction candle with wild high and low
        d += timedelta(days=1)
    today = d
    while today.weekday() >= 5:
        today += timedelta(days=1)
    ra = odds.yesterday_range_atr(bars, today)
    assert ra is not None and 0.8 < ra < 1.2                        # normal day, not "wide" because of the auction
    y = odds.yesterday_for(bars, today)
    assert y["close"] == 101                                       # the close still comes from the last candle


def test_trailing_exit_moves_the_stop_after_one_r():
    from app.engine import trail_stop
    d = date(2026, 11, 4)
    t0 = ist(d, hm("10:00"))
    p = {"side": "CE", "stop_index": 100.0, "entry_index": 110.0, "risk_pts": 10.0, "signal_time": t0.isoformat()}
    bars = [Bar(t0 + timedelta(minutes=5 * k), 0, h, 0, 0, 0, True, 5) for k, h in enumerate((112, 119.9))]
    assert trail_stop(p, bars) == (100.0, False)                 # not yet +1R on a closed candle
    bars.append(Bar(t0 + timedelta(minutes=10), 0, 126, 0, 0, 0, True, 5))
    assert trail_stop(p, bars) == (116.0, True)                  # best 126 -> stop 1R (10) behind
    bars.append(Bar(t0 + timedelta(minutes=15), 0, 140, 0, 0, 0, False, 5))
    assert trail_stop(p, bars) == (116.0, True)                  # the candle still forming does not count
    q = dict(p, side="PE", stop_index=120.0, entry_index=110.0)
    lows = [Bar(t0 + timedelta(minutes=5 * k), 0, 0, lo, 0, 0, True, 5) for k, lo in enumerate((105, 97))]
    assert trail_stop(q, lows) == (107.0, True)


def test_backtest_trailing_exit_runs(tmp_path):
    from dataclasses import replace
    from test_engine_server import make_demo
    s, e, src, j = make_demo(tmp_path)
    key = "NIFTY"
    bars = demomod.synthetic_history(key, src.day, 60, 3)
    sessions = odds.full_sessions(bars)
    ctx = odds.Ctx(key, 65, 50.0, "NSE")
    a = odds.backtest(sessions, replace(s, exit_mode="hold"), ctx, keep=None)
    b = odds.backtest(sessions, replace(s, exit_mode="trail"), ctx, keep=None)
    assert a["summary"]["fired"] == b["summary"]["fired"] > 0
    assert all(t["outcome"] in ("stop", "neither") for t in b["trades"])
    assert any(ta["exit"] != tb["exit"] for ta, tb in zip(a["trades"], b["trades"]))


def _st_research(h, l, c, n=10, mult=3.0):
    """the research lab's numpy version, copied as is"""
    import numpy as np
    h, l, c = map(np.asarray, (h, l, c))
    def ema(x, k):
        a = 2 / (k + 1); out = np.empty_like(x); out[0] = x[0]
        for i in range(1, len(x)):
            out[i] = a * x[i] + (1 - a) * out[i - 1]
        return out
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1)))); tr[0] = h[0] - l[0]
    atr = ema(tr, 2 * n - 1); mid = (h + l) / 2; ub, lb = mid + mult * atr, mid - mult * atr
    st = np.zeros(len(c)); d = np.ones(len(c)); fub, flb = ub.copy(), lb.copy()
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        d[i] = (-1 if c[i] < flb[i] else 1) if d[i - 1] == 1 else (1 if c[i] > fub[i] else -1)
        st[i] = flb[i] if d[i] == 1 else fub[i]
    return st, d


def test_supertrend_strategy_matches_research_and_paper_trades(tmp_path):
    import random
    from app.supertrend import supertrend
    random.seed(4)
    px = [100.0]
    for _ in range(400):
        px.append(px[-1] * (1 + random.gauss(0, 0.004)))
    h = [p * 1.002 for p in px]; l = [p * 0.998 for p in px]
    line, d = supertrend(h, l, px)
    rl, rd = _st_research(h, l, px)
    assert list(d) == [int(x) for x in rd] and all(abs(a - b) < 1e-6 for a, b in zip(line, rl))

    from test_engine_server import make_demo, run_until
    s, e, src, j = make_demo(tmp_path)
    run_until(src, "15:05")
    snap = e.supertrend.snapshot()
    assert set(snap) == {"NIFTY", "SENSEX"}
    fired = [k for k, v in snap.items() if v["signal"]]
    for k in fired:
        v = snap[k]
        t = v["signal"]["time"]
        assert "09:45" <= t <= "14:00" and v["signal"]["side"] in ("CE", "PE")
        assert v["plan"]["strategy"] == "supertrend" and v["plan"]["stop_index"] == pytest.approx(v["signal"]["stop"], abs=0.1)
        assert v["trade"]["status"] == "closed" and v["trade"]["exit_reason"] in ("stop", "time")
        assert j.find_open(src.day, k, e.mode, "supertrend")["id"] == v["trade"]["id"]
        assert j.find_open(src.day, k, e.mode)["id"] != v["trade"]["id"]      # the opening-range trade is separate
        # the signal is the first flip from 09:45 to 14:00 of the same series the research used
        today = src.day
        H, L, C = e.supertrend._history15(k, today)
        t15 = e.supertrend._today15(e.idx[k], today)
        _, rd = _st_research(H + [x[1] for x in t15], L + [x[2] for x in t15], C + [x[3] for x in t15])
        rd = list(rd[-len(t15):])
        first = next(x[0].strftime("%H:%M") for i, x in enumerate(t15) if i >= 1 and "09:45" <= x[0].strftime("%H:%M") <= "14:00"
                     and rd[i] != rd[i - 1])
        assert first == t


def test_supertrend_flip_makes_a_plan_and_a_paper_trade(tmp_path):
    from app.strategy import Quote
    from test_engine_server import make_demo
    s, e, src, j = make_demo(tmp_path)
    key, d = "NIFTY", src.day
    st = e.idx[key]
    hist = [b for b in e.hist_bars[key] if b.start.date() < d]
    p = hist[-1].c
    bars = []
    for k in range(72):                                    # up into 10:30, then a steady slide
        t = ist(d, hm("09:15")) + timedelta(minutes=5 * k)
        c = p * (1 + (0.0006 if k < 15 else -0.0025))
        bars.append(Bar(t, p, max(p, c) * 1.0003, min(p, c) * 0.9997, c, 0, True, 5))
        p = c
    seen = None
    for k in range(1, 72):
        now = ist(d, hm("09:15")) + timedelta(minutes=5 * k, seconds=1)
        src.clock.t = now
        st.c5.bars = bars[:k]
        st.ltp = bars[k - 1].c
        for x in e.idx.values():
            x.last_tick = now
        S = e.supertrend.state[key]
        if S["plan"]:
            tok = S["plan"]["contract"]["token"]
            e.quotes[tok] = Quote(ltp=150.0, bid=149.5, ask=150.5)
        e.evaluate()
        if S["signal"] and seen is None:
            seen = S["signal"]["time"]
    S = e.supertrend.state[key]
    assert seen and "09:45" <= seen <= "14:00" and S["signal"]["side"] == "PE"
    assert S["plan"]["side"] == "PE" and S["plan"]["stop_index"] > S["signal"]["close"]
    tr = j.get(S["trade_id"])
    assert tr["status"] == "closed" and tr["exit_reason"] in ("stop", "time")
    rows = [r for r in j.list(e.mode) if r["day"] == d.isoformat() and r["idx"] == key]
    assert any(r.get("strategy") == "supertrend" for r in rows)
