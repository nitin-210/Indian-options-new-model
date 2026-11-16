"""The revised rule (Nov 2026 research): a stronger breakout close and no fixed target."""
from dataclasses import replace
from datetime import date

from app import odds
from app.config import Settings
from app.strategy import find_signal, opening_range

from conftest import DAY, at, bars5

RANGE = [(25600, 25640, 25590, 25630), (25630, 25650, 25610, 25620), (25620, 25635, 25580, 25600)]
# Range: high 25650, low 25580, width 70; with buffer 0.2 a close must clear the range by 14 points


def test_defaults_are_the_revised_rule():
    s = Settings()
    assert s.breakout_buffer == 0.2 and s.exit_mode == "trail" and not s.validate()
    assert any("breakout_buffer" in e for e in Settings(breakout_buffer=1.5).validate())
    assert any("exit_mode" in e for e in Settings(exit_mode="bogus").validate())


def test_close_must_clear_the_range_by_the_buffer(s):
    s2 = replace(s, breakout_buffer=0.2)
    weak = (25650, 25670, 25645, 25660)    # 10 points above the high: not enough (needs > 25664)
    strong = (25660, 25680, 25655, 25668)  # 18 points above: signal
    bars = bars5(DAY, RANGE + [weak, strong])
    orng = opening_range(bars, DAY, s2, at(DAY, "09:31"))
    state, sig = find_signal("NIFTY", bars, orng, DAY, s2, at(DAY, "09:41"))
    assert state == "signal" and sig.side == "CE" and sig.close == 25668 and sig.time == at(DAY, "09:40")
    # the original rule would have taken the weak close
    state0, sig0 = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:41"))
    assert sig0.close == 25660 and sig0.time == at(DAY, "09:35")
    # and on the downside
    down = bars5(DAY, RANGE + [(25580, 25585, 25560, 25570), (25570, 25572, 25550, 25562)])
    o2 = opening_range(down, DAY, s2, at(DAY, "09:31"))
    st2, sg2 = find_signal("NIFTY", down, o2, DAY, s2, at(DAY, "09:41"))
    assert st2 == "signal" and sg2.side == "PE" and sg2.close == 25562


def _session(day, after):
    flat = (25700, 25705, 25695, 25700)
    rows = RANGE + after
    rows += [flat] * (72 - len(rows))
    return bars5(day, rows)


def test_hold_mode_never_exits_at_the_target(s):
    d = date(2026, 10, 5)
    # strong CE close 25670 (risk to mid 25615 = 55), then a run far beyond 2R (25780) that ends at 25700
    sess = {d: _session(d, [(25650, 25672, 25648, 25670), (25670, 25800, 25668, 25790)])}
    ctx = odds.Ctx("NIFTY", 65, 50.0, "nse")
    tgt = odds.backtest(sess, replace(s, breakout_buffer=0.2), ctx)["trades"][0]
    hold = odds.backtest(sess, replace(s, breakout_buffer=0.2, exit_mode="hold"), ctx)
    t = hold["trades"][0]
    assert tgt["outcome"] == "target"
    assert t["outcome"] == "neither" and t["exit"] == 25700        # held to 15:00
    assert "Held to the time exit" in hold["summary"]["verdict"] or "Too few" in hold["summary"]["verdict"]


def test_monte_carlo_hold_mode_has_no_target_hits(s):
    import numpy as np
    from app.timeutil import ist, hm
    rng = np.random.default_rng(1)
    pool = np.ones((30, odds.BARS_PER_SESSION, 4))
    pool[:, :, 1] += rng.uniform(0, 0.004, pool.shape[:2])
    pool[:, :, 2] -= rng.uniform(0, 0.004, pool.shape[:2])
    pool[:, :, 3] += rng.normal(0, 0.002, pool.shape[:2])
    st = replace(s, exit_mode="hold", mc_replays=2000)
    mc = odds.monte_carlo(pool, "CE", 25670.0, 25615.0, 25780.0, ist(DAY, hm("09:40")), st)
    assert mc["target_first"] == 0.0 and mc["stop_first"] > 0 and "held to the time exit" in mc["verdict"]
