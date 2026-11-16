import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.candles import Bar  # noqa: E402
from app.config import Settings  # noqa: E402
from app.instruments import Contract  # noqa: E402
from app.timeutil import hm, ist  # noqa: E402

DAY = date(2026, 11, 11)  # a Wednesday, not a holiday


def bars5(day: date, ohlc: list[tuple], start: str = "09:15", closed: bool = True) -> list[Bar]:
    t0 = ist(day, hm(start))
    return [Bar(t0 + timedelta(minutes=5 * i), o, h, l, c, 0.0, closed, 5) for i, (o, h, l, c) in enumerate(ohlc)]


def at(day: date, hhmm: str, sec: int = 0) -> datetime:
    return ist(day, hm(hhmm)) + timedelta(seconds=sec)


def contracts(day: date = DAY) -> list[Contract]:
    out = []
    n = 0
    exps_n = [date(2026, 11, 17), date(2026, 11, 23)]
    exps_s = [date(2026, 11, 12), date(2026, 11, 19)]
    for name, exps, lo, hi, step, lot, ex in (("NIFTY", exps_n, 25000, 26200, 50, 65, "NFO"),
                                               ("SENSEX", exps_s, 82000, 85600, 100, 20, "BFO")):
        for e in exps:
            for k in range(lo, hi + step, step):
                for opt in ("CE", "PE"):
                    n += 1
                    out.append(Contract(f"T{n}", f"{name}{k}{opt}", name, e, float(k), opt, lot, 0.05, ex))
    return out


@pytest.fixture
def s() -> Settings:
    """The original rule (any close beyond the range, exit at the 2R target): the mechanics tests use it."""
    st = Settings(breakout_buffer=0.0, exit_mode="target", trend_filter="none")
    st.max_loss_confirmed = True
    return st
