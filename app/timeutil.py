"""Clock, IST/UK conversion and the NSE/BSE trading calendar."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UK = ZoneInfo("Europe/London")

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
INDEX_AUCTION = time(15, 15)  # since 3 Aug 2026 F&O stocks (so the indices) stop at 15:15 for the closing auction
AUCTION_FROM = date(2026, 8, 3)  # first day of the closing auction session
PRE_OPEN = time(9, 0)


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def fmt_hm(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%H:%M")


def fmt_both(dt: datetime, seconds: bool = False) -> str:
    """'09:30 IST (04:00 UK)'."""
    f = "%H:%M:%S" if seconds else "%H:%M"
    return f"{dt.astimezone(IST).strftime(f)} IST ({dt.astimezone(UK).strftime(f)} UK)"


def fmt_day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


@dataclass
class Calendar:
    holidays: dict[date, str] = field(default_factory=dict)
    special_sessions: dict[date, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Calendar":
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            raw = tomllib.load(f)
        hol = {date.fromisoformat(k): v for k, v in raw.get("holidays", {}).items()}
        spec = {date.fromisoformat(k): v for k, v in raw.get("special_sessions", {}).items()}
        return cls(hol, spec)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def holiday_name(self, d: date) -> str | None:
        if d.weekday() >= 5:
            return self.special_sessions.get(d) or "Weekend"
        return self.holidays.get(d)

    def next_trading_day(self, d: date) -> date:
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n

    def prev_trading_day(self, d: date) -> date:
        n = d - timedelta(days=1)
        while not self.is_trading_day(n):
            n -= timedelta(days=1)
        return n

    def phase(self, now: datetime) -> str:
        """closed | pre-open | open | post-close (in IST)."""
        n = now.astimezone(IST)
        if not self.is_trading_day(n.date()):
            return "closed"
        t = n.time()
        if t < PRE_OPEN:
            return "closed"
        if t < SESSION_OPEN:
            return "pre-open"
        if t < SESSION_CLOSE:
            return "open"
        return "post-close"

    def next_open(self, now: datetime) -> datetime:
        n = now.astimezone(IST)
        d = n.date()
        if self.is_trading_day(d) and n.time() < SESSION_OPEN:
            return ist(d, SESSION_OPEN)
        return ist(self.next_trading_day(d), SESSION_OPEN)


def humanize_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
