"""History download: partial progress survives Angel refusing a request part-way."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

import app.angel as angel_mod
from app.angel import AngelClient, AngelError
from app.history import load_cached, update_history
from app.timeutil import IST, now_ist


class FakeClient:
    """Serves one 09:15 bar per day back to `oldest`; raises after `fail_after` requests."""
    candles_paged = AngelClient.candles_paged

    def __init__(self, oldest: datetime, fail_after: int | None = None):
        self.oldest = oldest
        self.fail_after = fail_after
        self.calls = 0

    async def candles(self, exchange, token, interval, start, end):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise AngelError("Unexpected reply from Angel One (HTTP 403)")
        out = []
        d = max(start, self.oldest).astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0)
        while d <= end:
            if d >= start:
                out.append((d, 100.0, 101.0, 99.0, 100.5, 0.0))
            d += timedelta(days=1)
        return out


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr(angel_mod.asyncio, "sleep", no_wait)


def run(coro):
    return asyncio.run(coro)


def test_refusal_keeps_what_was_downloaded_and_resumes(tmp_path):
    oldest = now_ist() - timedelta(days=700)
    flaky = FakeClient(oldest, fail_after=3)
    bars, note = run(update_history(flaky, "VIX", "NSE", "1", tmp_path, 1000))
    assert bars, "partial download must be kept"
    assert "stopped going further back" in note
    assert load_cached(tmp_path, "VIX") == bars           # saved to disk
    first_try_start = bars[0].start

    healthy = FakeClient(oldest)
    bars2, note2 = run(update_history(healthy, "VIX", "NSE", "1", tmp_path, 1000))
    assert bars2[0].start < first_try_start                # carried on further back
    assert bars2[0].start.date() == oldest.astimezone(IST).date()
    assert len({b.start for b in bars2}) == len(bars2)     # no duplicates
    meta = json.loads((tmp_path / "VIX_meta.json").read_text())
    assert meta.get("exhausted") is True                   # reached Angel's edge
    assert note2 == ""

    # Once exhausted, a later run only asks for the newest days.
    again = FakeClient(oldest)
    run(update_history(again, "VIX", "NSE", "1", tmp_path, 1000))
    assert again.calls <= 2


def test_same_refusal_twice_is_treated_as_the_edge(tmp_path):
    oldest = now_ist() - timedelta(days=2000)
    run(update_history(FakeClient(oldest, fail_after=3), "SENSEX", "BSE", "1", tmp_path, 1500))
    edge_client = FakeClient(oldest, fail_after=1)       # forward update ok, then refused again
    _, note = run(update_history(edge_client, "SENSEX", "BSE", "1", tmp_path, 1500))
    assert "will not give data older than" in note
    assert json.loads((tmp_path / "SENSEX_meta.json").read_text())["exhausted"] is True


def test_nothing_downloaded_raises(tmp_path):
    broken = FakeClient(now_ist() - timedelta(days=100), fail_after=0)
    with pytest.raises(AngelError):
        run(update_history(broken, "NIFTY", "NSE", "1", tmp_path, 200))


def test_paged_returns_partial_rows_and_status():
    end = datetime(2026, 11, 6, 15, 30, tzinfo=IST)
    c = FakeClient(end - timedelta(days=1000), fail_after=2)
    rows, status = run(c.candles_paged("NSE", "1", "FIVE_MINUTE", 900, 90, end))
    assert rows and status.startswith("stopped:")
    assert c.calls == 2 + 3                                # two good chunks, then three tries


def test_days_since_the_closing_auction_still_count_as_full_sessions():
    from datetime import date
    from app.candles import Bar
    from app.odds import BARS_PER_SESSION, describe_day, full_sessions
    from app.timeutil import ist, hm

    def day(d, last="15:25", gap=None, extra=(), dup=False):
        out, t = [], ist(d, hm("09:15"))
        end = ist(d, hm(last))
        while t <= end:
            if not (gap and hm(gap[0]) <= t.time() <= hm(gap[1])):
                out += [Bar(t, 1, 2, 0.5, 1.5, 0, True, 5)] * (2 if dup else 1)
            t += timedelta(minutes=5)
        out += [Bar(ist(d, hm(x)), 1, 2, 0.5, 1.5, 0, True, 5) for x in extra]
        return out

    old_day, auction_day, dup_day, short_day = (date(2026, 7, 31), date(2026, 8, 3), date(2026, 8, 4),
                                                date(2026, 8, 5))
    bars = (day(old_day) + day(auction_day, last="15:10", extra=("15:35",)) + day(dup_day, last="15:10", dup=True)
            + day(short_day, last="15:10", gap=("11:00", "11:05")))
    got = full_sessions(bars)
    assert set(got) == {old_day, auction_day, dup_day}
    assert all(len(v) == BARS_PER_SESSION == 72 for v in got.values())
    assert got[old_day][-1].start.time() == hm("15:10")
    assert "missing 11:00, 11:05" in describe_day(bars, short_day)


def test_time_exit_after_the_auction_start_is_rejected():
    from app.config import Settings
    assert any("15:10" in e for e in Settings(time_exit="15:20").validate())
    assert not Settings().validate()
