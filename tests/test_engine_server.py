from datetime import date

import pytest

from app import demo as demomod
from app.config import Settings
from app.engine import Engine, Hub, plan_text, rupee
from app.journal import Journal
from app.sources import DemoSource, SimClock
from app.timeutil import Calendar, hm, ist

from conftest import at


def make_demo(tmp_path, history=True, **settings):
    s = Settings(**{"exit_mode": "hold", **settings})     # the A/B version tests below are for the "hold" exit
    clock = SimClock(ist(demomod.DEMO_DAY, hm("09:12")), 10)
    cal = Calendar({date(2026, 11, 10): "Diwali Balipratipada"})
    j = Journal(tmp_path / "j.db")
    e = Engine(s, cal, j, "demo", Hub(), clock)
    src = DemoSource(e, s, clock)
    e.on_subscriptions = src._on_want
    e.set_contracts(src._contracts)
    e.set_stocks([x[0] for x in src._stocks], src._stock_opts, "fictional demo stocks")
    for k in ("NIFTY", "SENSEX"):
        e.seed_index(k, float(src.paths[k][0]), src.prev_close[k])
    e.evaluate()
    if history:
        for i, k in enumerate(("NIFTY", "SENSEX")):
            e.set_history(k, demomod.synthetic_history(k, src.day, 40, 3 + i), demomod.synthetic_vix(src.day, 40, 3), "synthetic")
    clock.t = src._start
    return s, e, src, j


def run_until(src, hhmm):
    target = ist(src.day, hm(hhmm))
    while src.clock.t < target and src.clock.playing:
        src.advance(60)


def test_demo_day_end_to_end(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    run_until(src, "10:00")
    n = e.idx["NIFTY"]
    assert n.signal and n.signal.side == "CE" and n.signal.time == ist(src.day, hm("09:50"))
    assert n.plan["contract"]["expiry"] == "2026-11-09"  # nearest NIFTY expiry (moved to Monday)
    assert n.risk["verdict"] == "PASS"
    assert n.plan["headline"].startswith("BUY NIFTY") and "CE" in n.plan["headline"]
    run_until(src, "15:35")
    sx = e.idx["SENSEX"]
    assert sx.signal.side == "PE"
    trades = {t["idx"]: t for t in j.list("demo")}
    # default rule holds the winner to 15:00 (no fixed target)
    assert trades["NIFTY"]["status"] == "closed" and trades["NIFTY"]["exit_reason"] == "time"
    assert trades["NIFTY"]["pnl"] > 0
    assert trades["SENSEX"]["exit_reason"] == "stop" and trades["SENSEX"]["pnl"] < 0
    # both exit versions are tracked: NIFTY's version B sold half at +1R; SENSEX was stopped before +1R
    vn, vs = trades["NIFTY"]["versions"], trades["SENSEX"]["versions"]
    assert vn["B"]["half"] and vn["A"]["pnl"] == trades["NIFTY"]["pnl"] and vn["B"]["pnl"] > 0
    assert vs["A"]["pnl"] == vs["B"]["pnl"] < 0
    assert e.idx["NIFTY"].yday["side"] == "CE" and e.idx["SENSEX"].yday["side"] == "PE"
    kinds = [ev["text"] for ev in e.events if ev["kind"] == "feed"]
    assert any("delayed" in k for k in kinds) and any("disconnected" in k for k in kinds)
    st = e.state_json()
    assert st["today"]["trades"] == 2
    ch = e.chain_json("NIFTY")
    assert len(ch["rows"]) == 21 and any(r["atm"] for r in ch["rows"])


def test_stale_feed_blocks_and_does_not_exit(tmp_path):
    s, e, src, j = make_demo(tmp_path, history=False)
    run_until(src, "09:51")
    n = e.idx["NIFTY"]
    assert n.trade and n.trade["status"] == "open"
    e.source_connected = False  # the feed drops
    e.evaluate()
    st = e.state_json()
    assert st["feed"]["state"] == "disconnected" and not st["feed"]["live"]
    assert j.get(n.trade_id)["status"] == "open"  # never exits on stale data


def test_rupee_and_plan_text():
    assert rupee(160000) == "₹1,60,000" and rupee(-1600) == "-₹1,600" and rupee(950) == "₹950"
    p = {"index": "NIFTY", "side": "CE", "contract": {"strike": 25600.0, "opt": "CE", "expiry": "2026-11-17"},
         "exit_time": "2026-11-11T15:00:00+05:30", "lots": 1, "qty": 65, "lot": 65, "target_key": "2R",
         "entry_fill": 120.2, "stop_index": 25540.0, "exit_fill_stop": 95.1, "target_index": 25720.0,
         "exit_fill_target": 165.3, "loss_at_stop": 1600.4, "one_lot_loss": 1600.4, "gain_at_target": 2800.0,
         "entry_limit": 120.25, "max_entry_premium": 131.5}
    t = plan_text(p)
    assert t["headline"] == "BUY NIFTY 25,600 CE (17 Nov) at about ₹120"
    assert t["stop_text"] == "exit if NIFTY falls to 25,540.00 (≈ ₹95)"
    assert t["size_text"] == "1 lot (65)"
    assert t["exit_text"] == "Exit by 15:00 IST (09:30 UK)"
    assert "₹1,600" in t["loss_text"]
    assert t["entry_text"] == "limit ₹120.25 · skip above ₹131.50"


def test_journal_lifecycle(tmp_path):
    j = Journal(tmp_path / "j.db")
    plan = {"index": "NIFTY", "side": "CE", "contract": {"symbol": "X", "token": "1", "lot": 65},
            "signal_time": "2026-11-11T09:50:00+05:30", "entry_index": 1, "stop_index": 0, "target_index": 2,
            "lots": 1, "entry_fill": 100.0, "loss_at_stop": 1500.0, "one_lot_loss": 1500.0}
    d = date(2026, 11, 11)
    a = j.open_trade(day=d, plan=plan, verdict="PASS", reasons=[], mode="demo")
    b = j.open_trade(day=d, plan=plan, verdict="BLOCK", reasons=["x"], mode="demo")
    assert j.today(d, "demo") == {"trades": 1, "realized": 0, "open_risk": 1500.0}
    j.close_trade(a, exit_time=at(d, "11:00"), exit_index=2, exit_fill=130.0, reason="target", costs=60.0)
    t = j.get(a)
    assert t["pnl"] == pytest.approx(65 * 30 - 60) and t["r_mult"] == pytest.approx((65 * 30 - 60) / 1500)
    j.promote(b, 101.0)
    assert j.get(b)["kind"] == "paper" and j.get(b)["entry_fill"] == 101.0
    st = j.stats("demo")
    assert st["paper"]["signals"] == 2 and st["paper"]["wins"] == 1 and st["equity"][0]["cum"] == pytest.approx(1890)
    j.delete_day(d, "demo")
    assert j.list("demo") == []


def test_server_is_local_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.config as cfg
    from app.server import Runtime, create_app

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.toml")
    s, e, src, j = make_demo(tmp_path, history=False)
    rt = Runtime(s, e, src, e.hub, j, 8765)
    client = TestClient(create_app(rt, start_background=False))
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/state", headers={"host": "evil.example:8765"}).status_code == 403
    assert client.post("/api/settings", json={"max_loss_per_trade": 1000},
                       headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/settings", content="max_loss_per_trade=1",
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
    r = client.post("/api/settings", json={"target_r": 3})
    assert r.status_code == 400
    r = client.post("/api/settings", json={"max_loss_per_trade": 1500, "daily_loss_limit": 4500})
    assert r.status_code == 200 and r.json()["settings"]["max_loss_confirmed"] is True
    assert (tmp_path / "settings.toml").exists()
    st = client.get("/api/state").json()
    assert "secret" not in str(st).lower() and "api_key" not in str(st)
    assert client.get("/").status_code == 200
    name = sorted(e.movers.stocks)[0]
    assert client.get("/api/stock", params={"name": "NOPE"}).status_code == 404
    r = client.get("/api/stock", params={"name": name, "tf": 1})
    assert r.status_code == 200 and r.json()["name"] == name and "bars" in r.json()
    assert client.post("/api/mover", json={"name": name}).status_code == 200 and e.chart_stock == name


def test_clicked_stock_gets_a_chart(tmp_path):
    from datetime import timedelta
    from app.candles import Bar
    s, e, src, j = make_demo(tmp_path, history=False)
    run_until(src, "10:30")
    name = sorted(e.movers.stocks)[0]
    calls = []
    e.on_stock_chart = calls.append
    assert e.select_mover(name) and calls == [name] and e.chart_stock == name
    d = e.stock_json(name, 5)
    assert d["today"] and len(d["bars"]) >= 14 and d["or"]["complete"] and d["or"]["high"] >= d["or"]["low"]
    assert len(e.stock_json(name, 1)["bars"]) >= 70
    lb = e.latest_bars()
    assert lb["STOCK"]["name"] == name and lb["STOCK"]["5"]
    # history fill for an earlier session (app started after the close / on a holiday)
    prev = src.day - timedelta(days=1)
    t0 = ist(prev, hm("09:15"))
    hist = [Bar(t0 + timedelta(minutes=i), 100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100.2 + i * 0.1, 10, True, 1)
            for i in range(375)]
    other = sorted(e.movers.stocks)[1]
    e.backfill_stock(other, hist, prev)
    d2 = e.stock_json(other, 5)
    assert not d2["today"] and d2["day"] == prev.isoformat() and len(d2["bars"]) == 75
    assert d2["or"]["low"] == 99.5 and e.chart_stock == name
    assert e.stock_json("NOPE", 5) is None


def test_live_source_fills_a_clicked_stock_after_hours(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta
    import app.sources as srcmod
    from app.config import Secret, Secrets
    from app.movers import FnoStock
    s, e, src, j = make_demo(tmp_path, history=False)
    stock = FnoStock("TESTCO", "123", "456", "TESTCOFUT", date(2026, 11, 24), 500)
    e.set_stocks([stock], [], "")
    live = srcmod.LiveSource(e, s, Secrets(*(Secret("x") for _ in range(5))))
    asked = {}

    async def fake_candles(exchange, token, interval, start, end):
        asked.update(exchange=exchange, token=token, interval=interval, start=start, end=end)
        return [(start + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5, 5.0) for i in range(375)]

    live.client.candles = fake_candles
    # Friday 22:08 IST, after the close: today's session is charted
    monkeypatch.setattr(srcmod, "now_ist", lambda: datetime(2026, 11, 6, 22, 8, tzinfo=srcmod.IST))
    e.clock = lambda: datetime(2026, 11, 6, 22, 8, tzinfo=srcmod.IST)
    asyncio.run(live._fill_stock("TESTCO"))
    assert asked["exchange"] == "NSE" and asked["token"] == "123" and asked["interval"] == "ONE_MINUTE"
    assert asked["start"] == ist(date(2026, 11, 6), hm("09:15")) and asked["end"] == ist(date(2026, 11, 6), hm("15:30"))
    d = e.stock_json("TESTCO", 5)
    assert d["today"] and len(d["bars"]) == 75
    # Saturday: the last session (Friday) is charted
    monkeypatch.setattr(srcmod, "now_ist", lambda: datetime(2026, 11, 7, 10, 0, tzinfo=srcmod.IST))
    asyncio.run(live._fill_stock("TESTCO"))
    assert asked["start"].date() == date(2026, 11, 6)
