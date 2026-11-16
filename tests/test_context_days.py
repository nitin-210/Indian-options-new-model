"""Earlier days on the chart: candles from the saved history / 1-minute context, and PDH / PDL / PDC levels."""
import json
from datetime import date, datetime, timedelta

import app.engine as engmod
from app import demo as demomod
from app.candles import Bar
from app.config import Settings
from app.engine import Engine, Hub
from app.journal import Journal
from app.sources import SimClock
from app.timeutil import IST, Calendar, hm, ist


def _session(d: date, base: float, step: float, minutes: int) -> list[Bar]:
    t0 = ist(d, hm("09:15"))
    return [Bar(t0 + timedelta(minutes=i * minutes), base + i * step, base + i * step + 2, base + i * step - 1,
                base + i * step + 1, 10, True, minutes) for i in range(375 // minutes)]


def test_context_days(tmp_path, monkeypatch):
    day = demomod.DEMO_DAY
    d1, d2, d3 = day - timedelta(days=1), day - timedelta(days=2), day - timedelta(days=3)
    hist = tmp_path / "history"
    hist.mkdir()
    rows = []
    for d, base in ((d3, 100.0), (d2, 200.0), (d1, 300.0), (day, 999.0)):   # today's bars must never be used
        rows += [[b.start.isoformat(), b.o, b.h, b.l, b.c, b.v] for b in _session(d, base, 1.0, 5)]
    (hist / "NIFTY_5m.json").write_text(json.dumps(rows))
    monkeypatch.setattr(engmod, "DATA_DIR", tmp_path)
    e = Engine(Settings(), Calendar({}), Journal(tmp_path / "j.db"), "demo", Hub(), SimClock(ist(day, hm("09:12")), 1))

    out = e.context_json("NIFTY", 5, 2)
    assert out["days"] == [d2.isoformat(), d1.isoformat()]
    assert len(out["bars"]) == 2 * 75
    lv = {v["label"]: v["price"] for v in out["levels"]}
    y = _session(d1, 300.0, 1.0, 5)
    assert lv == {"PDH": max(b.h for b in y), "PDL": min(b.l for b in y)}      # only yesterday's high and low
    assert len(e.context_json("NIFTY", 1, 2)["bars"]) == 2 * 75   # no 1-minute candles: the 5-minute ones instead
    assert e.context_json("NIFTY", 5, 0) == {"bars": [], "levels": [], "days": []}

    # 1-minute context (saved by the rebuild / fetched live) wins, and is grouped into 5-minute candles
    e.set_context1m("NIFTY", _session(d1, 500.0, 0.1, 1))
    one = e.context_json("NIFTY", 1, 1)
    assert len(one["bars"]) == 375
    five = e.context_json("NIFTY", 5, 1)
    assert len(five["bars"]) == 75 and five["levels"][0]["label"] == "PDH"
    assert five["levels"][0]["price"] == round(max(b.h for b in _session(d1, 500.0, 0.1, 1)), 2)
    assert e.context_json("NIFTY", 5, 5)["days"] == [d3.isoformat(), d2.isoformat(), d1.isoformat()]
