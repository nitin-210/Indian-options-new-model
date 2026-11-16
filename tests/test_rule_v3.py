"""v9: yesterday's-direction filter and exit version B (half at +1R, rest's stop to entry)."""
from dataclasses import replace
from datetime import date

from app import odds
from app.config import Settings
from app.strategy import find_signal, opening_range

from conftest import DAY, at, bars5

RANGE = [(25600, 25640, 25590, 25630), (25630, 25650, 25610, 25620), (25620, 25635, 25580, 25600)]


def _day(d, rows):
    flat = rows[-1][3]
    rows = rows + [(flat, flat + 2, flat - 2, flat)] * (72 - len(rows))
    return bars5(d, rows)


def test_defaults_include_the_trend_filter():
    s = Settings()
    assert s.trend_filter == "yesterday" and s.exit_mode == "trail" and s.half_at_r == 1.0 and not s.validate()
    assert any("trend_filter" in e for e in Settings(trend_filter="sma").validate())
    assert not Settings(exit_mode="half").validate()


def test_yesterday_direction_helpers():
    d0, d1 = date(2026, 11, 2), date(2026, 11, 3)
    bars = _day(d0, [(100, 101, 99, 100), (100, 103, 99, 102)]) + _day(d1, [(102, 103, 97, 98)])
    assert odds.yesterday_sides(bars) == {d1: "CE"}           # Nov 2 closed above its open
    y = odds.yesterday_for(bars, date(2026, 11, 4))
    assert y["date"] == d1 and y["side"] == "PE" and y["open"] == 102 and y["close"] == 98


def test_find_signal_skips_the_side_yesterday_does_not_allow(s):
    s2 = replace(s, breakout_buffer=0.2)
    down = (25580, 25582, 25550, 25560)   # PE breakout first...
    up = (25640, 25690, 25638, 25680)     # ...then a CE breakout
    bars = bars5(DAY, RANGE + [down, (25560, 25640, 25555, 25630), up])
    orng = opening_range(bars, DAY, s2, at(DAY, "09:31"))
    st, sig = find_signal("NIFTY", bars, orng, DAY, s2, at(DAY, "09:46"))
    assert sig.side == "PE"
    st, sig = find_signal("NIFTY", bars, orng, DAY, s2, at(DAY, "09:46"), allowed={"CE"})
    assert st == "signal" and sig.side == "CE" and sig.time == at(DAY, "09:45")


def test_backtest_applies_the_filter_and_version_b(s):
    d = date(2026, 10, 6)
    ctx = odds.Ctx("NIFTY", 65, 50.0, "nse")
    # CE close 25670 (stop mid 25615, risk 55): +1R = 25725 is reached, then back to entry
    sess = {d: _day(d, RANGE + [(25650, 25672, 25648, 25670), (25670, 25730, 25668, 25720),
                                (25720, 25722, 25660, 25665)])}
    base = replace(s, breakout_buffer=0.2, exit_mode="hold", trend_filter="yesterday")
    assert odds.backtest(sess, base, ctx, prev={d: "PE"})["trades"] == []        # yesterday down: no CE
    a = odds.backtest(sess, base, ctx, prev={d: "CE"})["trades"][0]
    b = odds.backtest(sess, replace(base, exit_mode="half"), ctx, prev={d: "CE"})["trades"][0]
    assert a["outcome"] == "neither"                                            # A held to 15:00
    assert b["outcome"] == "half+stop"                                          # B: half at +1R, rest stopped at entry
    assert abs(b["r_index"] - 0.5) < 1e-9                                       # 0.5 x 1R + 0.5 x 0R
    assert odds.backtest(sess, replace(base, trend_filter="none"), ctx, prev={d: "PE"})["trades"]  # filter off


def test_journal_tracks_versions(tmp_path):
    from app.journal import Journal
    j = Journal(tmp_path / "j.db")
    plan = {"index": "NIFTY", "side": "CE", "contract": {"symbol": "X", "token": "1", "lot": 65},
            "signal_time": "2026-11-09T09:50:00+05:30", "entry_index": 25670, "stop_index": 25615,
            "target_index": 25780, "lots": 1, "entry_fill": 150.0, "loss_at_stop": 1900.0, "one_lot_loss": 1900.0}
    tid = j.open_trade(day=date(2026, 11, 9), plan=plan, verdict="PASS", reasons=[], mode="live")
    j.set_versions(tid, {"A": {"open": False, "pnl": 2500.0, "r": 1.3}, "B": {"open": False, "pnl": 1800.0, "r": 0.9}})
    from datetime import datetime
    j.close_trade(tid, exit_time=datetime(2026, 11, 9, 15, 0), exit_index=25760, exit_fill=190.0, reason="time", costs=100)
    st = j.stats("live")
    assert st["versions"]["A"]["net_pnl"] == 2500.0 and st["versions"]["B"]["win_rate"] == 1.0
    assert j.list("live")[0]["versions"]["B"]["pnl"] == 1800.0
