import math
from datetime import date

import pytest

from app import bs
from app.costs import round_trip
from app.instruments import OptionUniverse
from app.strategy import (Quote, find_signal, index_levels, make_plan, opening_range, size_position)

from conftest import DAY, at, bars5, contracts

RANGE = [(25600, 25640, 25590, 25630), (25630, 25650, 25610, 25620), (25620, 25635, 25580, 25600)]
# Range: high 25650, low 25580, mid 25615


def test_opening_range_complete_only_after_0930(s):
    bars = bars5(DAY, RANGE)
    orng = opening_range(bars, DAY, s, at(DAY, "09:29"))
    assert orng.high == 25650 and orng.low == 25580 and orng.mid == 25615
    assert not orng.complete
    assert opening_range(bars, DAY, s, at(DAY, "09:30")).complete
    open_bar = bars5(DAY, RANGE, closed=False)
    assert not opening_range(open_bar, DAY, s, at(DAY, "09:31")).complete


def test_first_close_above_is_a_call_signal(s):
    bars = bars5(DAY, RANGE + [(25600, 25660, 25595, 25640),  # wick above, closes inside: no signal
                               (25640, 25670, 25630, 25650),  # closes AT the high: not above
                               (25650, 25680, 25645, 25672),  # closes above: signal
                               (25672, 25672, 25500, 25510)])  # later break below does not matter
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    state, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "10:00"))
    assert state == "signal" and sig.side == "CE" and sig.close == 25672
    assert sig.time == at(DAY, "09:45")  # the 09:40 bar closes at 09:45


def test_first_close_below_is_a_put_signal(s):
    bars = bars5(DAY, RANGE + [(25600, 25610, 25560, 25570)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    state, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    assert state == "signal" and sig.side == "PE" and sig.time == at(DAY, "09:35")


def test_unclosed_bar_never_signals(s):
    bars = bars5(DAY, RANGE) + bars5(DAY, [(25600, 25700, 25600, 25700)], start="09:30", closed=False)
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    assert find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:33"))[0] == "waiting"


def test_no_signals_after_1400(s):
    quiet = [(25600, 25640, 25590, 25600)] * 54  # the 09:30 .. 13:55 bars stay inside
    late = [(25600, 25700, 25600, 25700)]  # the 14:00 bar closes at 14:05
    bars = bars5(DAY, RANGE + quiet + late)
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    assert find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "14:06"))[0] == "expired"
    # but a bar that closes exactly at 14:00 still counts
    bars2 = bars5(DAY, RANGE + quiet[:-1] + late)
    state, sig = find_signal("NIFTY", bars2, opening_range(bars2, DAY, s, at(DAY, "09:31")), DAY, s, at(DAY, "14:01"))
    assert state == "signal" and sig.time == at(DAY, "14:00")


def test_stops_and_targets(s):
    bars = bars5(DAY, RANGE + [(25650, 25680, 25645, 25672)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    stop, t, risk = index_levels(sig, orng, s)
    assert stop == 25615 and risk == 57
    assert t == {"1R": 25729, "1.5R": 25757.5, "2R": 25786}
    s.stop_rule = "far"
    stop, t, risk = index_levels(sig, orng, s)
    assert stop == 25580 and risk == 92 and t["2R"] == 25856


def test_put_stop_rounds_up_away_from_entry(s):
    bars = bars5(DAY, [(100.0, 100.13, 100.0, 100.05), (100.05, 100.1, 99.9, 100.0), (100, 100, 99.92, 99.95),
                       (99.95, 99.95, 99.8, 99.85)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    stop, t, risk = index_levels(sig, orng, s)
    assert sig.side == "PE" and stop == 100.05  # mid 100.015 rounded UP to 0.05
    assert t["1R"] == pytest.approx(99.65)


def test_expiry_choice_and_strikes():
    u = OptionUniverse("NIFTY", contracts())
    assert u.choose_expiry(date(2026, 11, 11)) == (date(2026, 11, 17), [])
    exp, flags = u.choose_expiry(date(2026, 11, 17))  # expiry day: use the next one and say so
    assert exp == date(2026, 11, 23) and "expiry day" in flags[0]
    assert u.step(date(2026, 11, 17)) == 50
    assert u.atm(25672, date(2026, 11, 17)) == 25650
    assert u.atm(25676, date(2026, 11, 17)) == 25700
    assert u.pick_strike(25672, date(2026, 11, 17), "CE", "itm1") == 25600
    assert u.pick_strike(25672, date(2026, 11, 17), "PE", "itm1") == 25700
    chain = u.chain(25650, date(2026, 11, 17), 10)
    assert len(chain) == 21 and chain[0] == 25150 and chain[-1] == 26150


def test_sizing_counts_costs(s):
    s.max_loss_per_trade = 2000
    sz = size_position(120.0, 95.0, 65, "nse", s)
    # 25 drop x 65 = 1,625 a lot; 1 lot + costs fits, 2 lots would not
    assert sz.lots == 1 and sz.qty == 65
    expected = 65 * 25 + round_trip(120, 95, 65, "nse", s).total
    assert sz.loss_at_stop == pytest.approx(expected)
    assert sz.loss_at_stop <= 2000
    s.max_loss_per_trade = 1650  # 1,625 + about 70 of charges does not fit
    assert size_position(120.0, 95.0, 65, "nse", s).lots == 0
    s.max_loss_per_trade = 100000
    s.max_lots = 3
    assert size_position(120.0, 95.0, 65, "nse", s).lots == 3


def test_plan_premiums_move_the_right_way(s):
    s.max_loss_per_trade = 3000  # a 57-point stop costs about Rs 2,050 for one lot
    cs = contracts()
    u = OptionUniverse("NIFTY", cs)
    bars = bars5(DAY, RANGE + [(25650, 25680, 25645, 25672)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    c = u.contract(date(2026, 11, 17), 25650, "CE")
    now = at(DAY, "09:35", 5)
    T = bs.years_to_expiry(now, c.expiry)
    px = round(bs.price(25672, 25650, T, 0.06, 0.13, "CE"), 1)
    q = Quote(ltp=px, bid=px - 0.3, ask=px + 0.3)
    p = make_plan(sig, orng, c, q, 25672, now, s, "nse", [])
    assert p.iv == pytest.approx(0.13, abs=0.01)
    assert p.exit_fill_stop < p.entry_fill < p.exit_fill_target
    assert p.prem_targets["1R"] < p.prem_targets["1.5R"] < p.prem_targets["2R"]
    assert 0.4 < p.delta < 0.7 and p.theta_day < 0
    assert p.prem_time_exit_flat < p.premium_ltp  # time decay by 15:00
    assert p.entry_fill == pytest.approx(q.ask + 0.05)
    assert p.sizing.lots >= 1 and p.sizing.loss_at_stop <= s.max_loss_per_trade
    # A put plan: stop premium below entry too (puts lose when the index rises)
    bars_pe = bars5(DAY, RANGE + [(25600, 25610, 25560, 25570)])
    orng2 = opening_range(bars_pe, DAY, s, at(DAY, "09:31"))
    _, sig2 = find_signal("NIFTY", bars_pe, orng2, DAY, s, at(DAY, "09:36"))
    c2 = u.contract(date(2026, 11, 17), 25550, "PE")
    px2 = round(bs.price(25570, 25550, T, 0.06, 0.13, "PE"), 1)
    p2 = make_plan(sig2, orng2, c2, Quote(ltp=px2, bid=px2 - 0.3, ask=px2 + 0.3), 25570, now, s, "nse", [])
    assert p2.stop_index > p2.entry_index > p2.target_index
    assert p2.exit_fill_stop < p2.entry_fill < p2.exit_fill_target


def test_plan_without_live_quote_uses_fallback(s):
    u = OptionUniverse("NIFTY", contracts())
    bars = bars5(DAY, RANGE + [(25650, 25680, 25645, 25672)])
    orng = opening_range(bars, DAY, s, at(DAY, "09:31"))
    _, sig = find_signal("NIFTY", bars, orng, DAY, s, at(DAY, "09:36"))
    c = u.contract(date(2026, 11, 17), 25650, "CE")
    p = make_plan(sig, orng, c, Quote(), 25672, at(DAY, "09:35", 5), s, "nse", [])
    assert "default" in p.iv_source and p.notes
    assert math.isfinite(p.entry_fill) and p.entry_fill > 0
