import asyncio
import json
from datetime import timedelta

from app import demo as demomod
from app.config import Settings
from app.engine import Engine, Hub
from app.instruments import save_contracts
from app.journal import Journal
from app.sources import ReplaySource, SimClock
from app.timeutil import Calendar, hm, ist


def _minutes(path, day):
    t0 = ist(day, hm("09:15"))
    out = []
    for m in range(len(path) // 60):
        seg = path[m * 60: m * 60 + 60]
        out.append([(t0 + timedelta(minutes=m)).isoformat(), float(seg[0]), float(seg.max()), float(seg.min()),
                    float(seg[-1]), 1000.0 + m])
    return out


def test_rebuilt_day_replays_real_stocks(tmp_path):
    day = demomod.DEMO_DAY
    paths = {k: demomod.bridge_path(v["anchors"], v["vol"], seed=11 + i)
             for i, (k, v) in enumerate(demomod.SCENARIOS.items())}
    stocks, opts = demomod.demo_stocks(day)
    folder = tmp_path / "rebuilt"
    folder.mkdir()
    meta = {"day": day.isoformat(), "kind": "rebuilt", "vix": 14.0,
            "bars1m": {k: _minutes(p, day) for k, p in paths.items()},
            "prev_close": {k: v["prev_close"] for k, v in demomod.SCENARIOS.items()},
            "stocks": [{"name": s.name, "eq_token": s.eq_token, "fut_token": s.fut_token, "fut_symbol": s.fut_symbol,
                        "fut_expiry": s.fut_expiry.isoformat(), "lot": s.lot} for s, *_ in stocks]}
    (folder / "meta.json").write_text(json.dumps(meta))
    bars = {s.name: _minutes(path, day) for s, pc, path, *_ in stocks}
    (folder / "stock_bars1m.json").write_text(json.dumps(bars))
    daily = demomod.demo_stock_daily(stocks, day)
    (folder / "stock_daily.json").write_text(json.dumps({k: [[r[0].isoformat(), *r[1:]] for r in v]
                                                         for k, v in daily.items()}))
    save_contracts(opts, folder / "stock_options.json")

    clock = SimClock(ist(day, hm("09:12")), 10)
    e = Engine(Settings(), Calendar(), Journal(tmp_path / "j.db"), "replay", Hub(), clock)
    src = ReplaySource(e, e.s, clock, folder)

    async def no_loop():
        return None
    src.loop = no_loop
    asyncio.run(src.start())
    assert len(e.movers.stocks) == len(stocks) and "real F&O stocks" in e.movers.label
    assert set(e.stock_daily) == {s.name for s, *_ in stocks}
    clock.t = src._start
    while clock.t < ist(day, hm("10:30")):
        src.advance(60)
    snap = e.movers.snapshot(10, None)
    assert snap["priced"] == len(stocks) and snap["bull"] and snap["bear"]
    # the stock's 1-minute candles match the saved ones exactly (open, high, low, close)
    name = stocks[0][0].name
    c1 = e.stock_bars[name][0].bars
    saved = {r[0]: r for r in bars[name]}
    checked = 0
    for b in c1:
        r = saved.get(b.start.isoformat())
        if r and b.closed:
            assert (round(b.o, 2), round(b.h, 2), round(b.l, 2), round(b.c, 2)) == \
                   (round(r[1], 2), round(r[2], 2), round(r[3], 2), round(r[4], 2))
            checked += 1
    assert checked > 50
    from app.predict import Predictor
    picks = Predictor(e).picks(clock.t)
    assert picks and all(p["name"] in bars for p in picks)


def test_rebuild_downloads_stocks_and_resumes(tmp_path, monkeypatch):
    import app.__main__ as main
    import app.angel as angel
    from datetime import date, datetime

    day = date(2026, 11, 9)
    calls = {"n": 0, "refused": set()}

    class FakeSecrets:
        def problems(self):
            return []

    class FakeClient:
        def __init__(self, secrets):
            pass

        async def login(self):
            pass

        async def close(self):
            pass

        async def candles(self, exchange, token, interval, start, end):
            calls["n"] += 1
            if token == "E2" and token not in calls["refused"]:
                calls["refused"].add(token)      # refused once, works on the retry pass
                raise angel.AngelError("Candle request failed: Unexpected reply from Angel One (HTTP 403)")
            if interval == "ONE_DAY":
                return [(datetime(2026, 10, d, 0, 0, tzinfo=start.tzinfo), 100, 103, 98, 101, 0) for d in range(1, 31)
                        if date(2026, 10, d).weekday() < 5]
            t0 = ist(day, hm("09:15"))
            return [(t0 + timedelta(minutes=m), 100 + m * 0.1, 100.5 + m * 0.1, 99.5 + m * 0.1, 100.2 + m * 0.1, 500)
                    for m in range(375)]

    master = []
    for i, n in enumerate(("AAA", "BBB", "CCC")):
        master.append({"exch_seg": "NSE", "symbol": f"{n}-EQ", "name": n, "token": f"E{i}"})
        master.append({"exch_seg": "NFO", "instrumenttype": "FUTSTK", "symbol": f"{n}25NOV26FUT", "name": n,
                       "token": f"F{i}", "expiry": "25NOV2026", "lotsize": "500"})
        for k in (90, 100, 110, 200):
            for o in ("CE", "PE"):
                master.append({"exch_seg": "NFO", "instrumenttype": "OPTSTK", "symbol": f"{n}25NOV26{k}{o}",
                               "name": n, "token": f"O{i}{k}{o}", "expiry": "25NOV2026", "strike": str(k * 100),
                               "lotsize": "500", "tick_size": "5"})

    async def fake_master(cache_dir, today_iso):
        return master

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "load_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(angel, "AngelClient", FakeClient)
    monkeypatch.setattr(main, "_upstox_secrets", lambda: None)   # ignore a real [upstox] token in secrets.toml
    monkeypatch.setattr(angel, "download_master", fake_master)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)

    assert asyncio.run(main.rebuild(day)) == 0
    folder = tmp_path / "sessions" / "2026-11-09-rebuilt"
    meta = json.loads((folder / "meta.json").read_text())
    assert [s["name"] for s in meta["stocks"]] == ["AAA", "BBB", "CCC"]
    bars = json.loads((folder / "stock_bars1m.json").read_text())
    assert set(bars) == {"AAA", "BBB", "CCC"} and len(bars["CCC"]) == 375
    assert set(json.loads((folder / "stock_daily.json").read_text())) == {"AAA", "BBB", "CCC"}
    strikes = {c["strike"] for c in json.loads((folder / "stock_options.json").read_text())}
    assert strikes == {90.0, 100.0, 110.0}            # 200 is far outside the day's range: not kept
    # a second run reuses everything already saved: no new downloads
    n = calls["n"]
    assert asyncio.run(main.rebuild(day)) == 0
    assert calls["n"] == n
