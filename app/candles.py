"""Build 1-minute and 5-minute candles from ticks.

Bars are aligned to the session open (09:15 IST). A bar counts as closed
only once the clock has passed its end (start + interval); a price that pokes
through a level mid-bar is never a signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .timeutil import IST, SESSION_CLOSE, SESSION_OPEN


@dataclass
class Bar:
    start: datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    closed: bool = False
    minutes: int = 5

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    def to_json(self, shift_to_ist_epoch: bool = True) -> dict:
        ts = int(self.start.timestamp())
        if shift_to_ist_epoch:
            ts += 19800  # charts show IST on a UTC axis
        return {"time": ts, "open": round(self.o, 2), "high": round(self.h, 2),
                "low": round(self.l, 2), "close": round(self.c, 2), "volume": self.v,
                "closed": self.closed}


class CandleBuilder:
    def __init__(self, minutes: int, open_t: time = SESSION_OPEN, close_t: time = SESSION_CLOSE):
        self.minutes = minutes
        self.len = timedelta(minutes=minutes)
        self.open_t = open_t
        self.close_t = close_t
        self.bars: list[Bar] = []
        self._vol_base: float | None = None
        self._last_cum_vol: float | None = None

    def reset(self):
        self.bars.clear()
        self._vol_base = None
        self._last_cum_vol = None

    def bucket(self, ts: datetime) -> datetime | None:
        ts = ts.astimezone(IST)
        open_dt = datetime.combine(ts.date(), self.open_t, tzinfo=IST)
        close_dt = datetime.combine(ts.date(), self.close_t, tzinfo=IST)
        if ts < open_dt or ts >= close_dt:
            return None
        n = int((ts - open_dt).total_seconds() // self.len.total_seconds())
        return open_dt + n * self.len

    @property
    def current(self) -> Bar | None:
        return self.bars[-1] if self.bars and not self.bars[-1].closed else None

    def closed_bars(self) -> list[Bar]:
        return [b for b in self.bars if b.closed]

    def add(self, price: float, ts: datetime, cum_volume: float | None = None) -> list[Bar]:
        """Add a tick. Returns bars that this tick closed."""
        closed: list[Bar] = []
        b = self.bucket(ts)
        if b is None or price <= 0:
            return closed
        vol = 0.0
        if cum_volume is not None:
            if self._last_cum_vol is not None and cum_volume >= self._last_cum_vol:
                vol = cum_volume - self._last_cum_vol
            self._last_cum_vol = cum_volume
        last = self.bars[-1] if self.bars else None
        if last is not None and b < last.start:
            return closed  # late/out-of-order tick for an old bar: ignore
        if last is not None and b == last.start and not last.closed:
            last.h = max(last.h, price)
            last.l = min(last.l, price)
            last.c = price
            last.v += vol
            return closed
        if last is not None and not last.closed:
            last.closed = True
            closed.append(last)
        if last is not None and b == last.start and last.closed:
            return closed  # bar already finalised by the clock; drop straggler
        self.bars.append(Bar(b, price, price, price, price, vol, minutes=self.minutes))
        return closed

    def close_due(self, now: datetime, grace_s: float = 1.0) -> list[Bar]:
        """Close the forming bar once the clock is past its end (+ a small grace)."""
        cur = self.current
        if cur is not None and now >= cur.start + self.len + timedelta(seconds=grace_s):
            cur.closed = True
            return [cur]
        return []

    def load(self, bars: list[Bar]) -> None:
        self.bars = list(bars)
        for b in self.bars:
            b.minutes = self.minutes
