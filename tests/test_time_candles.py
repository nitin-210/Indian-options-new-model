from datetime import date

from app.candles import CandleBuilder
from app.timeutil import Calendar, fmt_both, ist, hm

from conftest import at


def test_ist_uk_in_winter_and_summer():
    assert fmt_both(ist(date(2026, 11, 9), hm("09:30"))) == "09:30 IST (04:00 UK)"
    assert fmt_both(ist(date(2026, 11, 9), hm("15:00"))) == "15:00 IST (09:30 UK)"
    # British Summer Time: UK is 4h30 behind IST
    assert fmt_both(ist(date(2027, 6, 9), hm("09:15"))) == "09:15 IST (04:45 UK)"
    # Clocks go forward on 28 March 2027
    assert fmt_both(ist(date(2027, 3, 26), hm("09:15"))) == "09:15 IST (03:45 UK)"
    assert fmt_both(ist(date(2027, 3, 29), hm("09:15"))) == "09:15 IST (04:45 UK)"


def test_calendar_holidays_and_phases():
    cal = Calendar({date(2026, 11, 10): "Diwali Balipratipada", date(2026, 11, 24): "Guru Nanak"})
    assert not cal.is_trading_day(date(2026, 11, 10))
    assert not cal.is_trading_day(date(2026, 11, 8))  # Sunday
    assert cal.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)
    assert cal.prev_trading_day(date(2026, 11, 11)) == date(2026, 11, 9)
    d = date(2026, 11, 11)
    assert cal.phase(at(d, "08:30")) == "closed"
    assert cal.phase(at(d, "09:05")) == "pre-open"
    assert cal.phase(at(d, "09:15")) == "open"
    assert cal.phase(at(d, "15:30")) == "post-close"
    assert cal.next_open(at(d, "16:00")) == ist(date(2026, 11, 12), hm("09:15"))


def test_five_minute_bars_from_ticks():
    d = date(2026, 11, 11)
    cb = CandleBuilder(5)
    assert cb.add(100, at(d, "09:14", 50)) == []  # pre-open ignored
    assert cb.bars == []
    cb.add(100, at(d, "09:15", 1))
    cb.add(102, at(d, "09:17"))
    cb.add(99, at(d, "09:19", 59))
    closed = cb.add(101, at(d, "09:20"))
    assert len(closed) == 1
    b = closed[0]
    assert (b.o, b.h, b.l, b.c, b.closed) == (100, 102, 99, 99, True)
    assert b.start == ist(d, hm("09:15")) and b.end == ist(d, hm("09:20"))
    assert cb.current.start == ist(d, hm("09:20"))


def test_clock_closes_bar_and_late_ticks_are_dropped():
    d = date(2026, 11, 11)
    cb = CandleBuilder(5)
    cb.add(100, at(d, "09:21"))
    assert cb.close_due(at(d, "09:25", 0)) == []  # grace not passed yet
    closed = cb.close_due(at(d, "09:25", 2))
    assert len(closed) == 1 and closed[0].closed
    cb.add(150, at(d, "09:24", 59))  # straggler for the closed bar
    assert cb.bars[-1].c == 100 and len(cb.bars) == 1
    cb.add(90, at(d, "15:30"))  # after the close
    assert len(cb.bars) == 1


def test_one_minute_alignment_and_volume():
    d = date(2026, 11, 11)
    cb = CandleBuilder(1)
    cb.add(10, at(d, "09:15", 5), cum_volume=1000)
    cb.add(11, at(d, "09:15", 30), cum_volume=1300)
    cb.add(12, at(d, "09:16", 1), cum_volume=1350)
    assert [b.start.strftime("%H:%M") for b in cb.bars] == ["09:15", "09:16"]
    assert cb.bars[0].v == 300 and cb.bars[1].v == 50
