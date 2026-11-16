import logging
from datetime import date, timedelta

import numpy as np
import pytest

from app import odds
from app.config import RedactingFilter, Secret, Settings, load_secrets, register_secret, redact, save_settings, load_settings
from app.feed import AngelFeed, build_packet, parse_packet
from app.instruments import OptionUniverse, parse_master

from conftest import at, bars5

RANGE = [(100, 101, 99.5, 100.5), (100.5, 101, 99.8, 100.2), (100.2, 100.6, 99.0, 99.5)]  # hi 101, lo 99, mid 100
FLAT = (100.5, 100.8, 100.2, 100.5)


def session(day, after):
    """75 bars: range, then `after`, then flat bars to the close."""
    rows = RANGE + after
    rows += [FLAT] * (75 - len(rows))
    return bars5(day, rows)


def _ctx():
    return odds.Ctx("NIFTY", 65, 50.0, "nse")


def test_backtest_counts_outcomes(s):
    d0 = date(2026, 10, 5)
    sessions = {
        d0: session(d0, [(100.9, 101.3, 100.9, 101.2), (101.2, 103.5, 101.1, 103.4)]),  # CE, risk 1.2 -> 2R 103.6? no
        d0 + timedelta(1): session(d0 + timedelta(1), [(99.2, 99.3, 98.6, 98.8), (98.8, 100.2, 98.8, 100.1)]),  # PE then stop
        d0 + timedelta(2): session(d0 + timedelta(2), []),  # no breakout
    }
    res = odds.backtest(sessions, s, _ctx())
    sm = res["summary"]
    assert sm["sessions"] == 3 and sm["fired"] == 2 and sm["no_signal"] == 1
    by_day = {t["date"]: t for t in res["trades"]}
    ce = by_day[d0.isoformat()]
    assert ce["side"] == "CE" and ce["entry"] == 101.2 and ce["stop"] == 100.0
    assert ce["outcome"] == "neither"  # 2R target 103.6 never reached; flat bars to 15:00
    pe = by_day[(d0 + timedelta(1)).isoformat()]
    assert pe["side"] == "PE" and pe["outcome"] == "stop" and pe["r_index"] == pytest.approx(-1.0)
    assert "estimate" in sm["note"].lower()


def test_same_bar_touching_both_counts_as_stop(s):
    s.target_r = 1.0
    d = date(2026, 10, 7)
    sess = {d: session(d, [(100.9, 101.3, 100.9, 101.2), (101.2, 102.6, 99.8, 101.0)])}
    t = odds.backtest(sess, s, _ctx())["trades"][0]
    assert t["outcome"] == "stop"


def test_gap_through_stop_exits_at_the_open(s):
    d = date(2026, 10, 8)
    sess = {d: session(d, [(100.9, 101.3, 100.9, 101.2), (99.5, 99.6, 99.4, 99.5)])}
    t = odds.backtest(sess, s, _ctx())["trades"][0]
    assert t["outcome"] == "stop" and t["exit"] == 99.5 and t["r_index"] < -1


def _pool(n=40, seed=1):
    rng = np.random.default_rng(seed)
    sessions = {}
    d = date(2026, 6, 1)
    for i in range(n):
        px, rows = 25600.0, []
        for _ in range(75):
            c = px * (1 + rng.normal(0, 0.0008))
            rows.append((px, max(px, c) * 1.0003, min(px, c) * 0.9997, c))
            px = c
        sessions[d + timedelta(days=i)] = bars5(d + timedelta(days=i), rows)
    return odds.build_pool(sessions)


def test_monte_carlo_is_reproducible_and_sums_to_100(s):
    pool = _pool()
    s.mc_replays = 3000
    sig_t = at(date(2026, 11, 11), "10:00")
    a = odds.monte_carlo(pool, "CE", 25600, 25560, 25680, sig_t, s)
    b = odds.monte_carlo(pool, "CE", 25600, 25560, 25680, sig_t, s)
    assert a["target_first"] == b["target_first"] and a["final_p50"] == b["final_p50"]
    assert a["target_first"] + a["stop_first"] + a["neither"] == pytest.approx(100, abs=0.2)
    s.mc_seed = 20
    c = odds.monte_carlo(pool, "CE", 25600, 25560, 25680, sig_t, s)
    assert c["final_p50"] != a["final_p50"]
    assert len(a["fan"]["p50"]) == a["bars"] == 60  # 10:00 to 15:00


def test_monte_carlo_needs_enough_history(s):
    out = odds.monte_carlo(_pool(5), "CE", 25600, 25560, 25680, at(date(2026, 11, 11), "10:00"), s)
    assert "error" in out


def test_history_expiry_rule():
    assert odds.weekly_expiry_for("NIFTY", date(2026, 11, 4)) == date(2026, 11, 10)
    assert odds.weekly_expiry_for("NIFTY", date(2026, 11, 10)) == date(2026, 11, 17)  # expiry day -> next
    assert odds.weekly_expiry_for("NIFTY", date(2025, 6, 2)).weekday() == 3  # Thursdays before Sep 2025
    assert odds.weekly_expiry_for("SENSEX", date(2026, 11, 4)) == date(2026, 11, 5)
    assert odds.weekly_expiry_for("SENSEX", date(2024, 6, 3)).weekday() == 4


def test_verdict_line():
    assert odds.verdict_line(50, 50, 1.0).startswith("Close to a coin flip")
    assert odds.verdict_line(34, 66, 2.0).startswith("Close to break-even")
    assert odds.verdict_line(70, 30, 1.0).startswith("Strongly toward the target")
    assert odds.verdict_line(1, 1, 1.0).startswith("Too few")


# ------------------------------------------------------------------ feed


def test_packet_roundtrip_all_modes():
    for mode in (1, 2, 3):
        b = build_packet(mode, 2, "45678", 1762312345000, 182.35, vol=12000, oi=4500000, oi_chg_pct=3.5,
                         close=160.0, bids=[(182.30, 650), (182.25, 1300)], asks=[(182.40, 520)])
        t = parse_packet(b)
        assert t["token"] == "45678" and t["ws_type"] == 2 and t["ltp"] == pytest.approx(182.35)
        assert t["ts"] == 1762312345000
        if mode >= 2:
            assert t["vol"] == 12000 and t["close"] == pytest.approx(160.0)
        if mode == 3:
            assert t["oi"] == 4500000 and t["oi_chg_pct"] == pytest.approx(3.5)
            assert t["bids"][0][0] == pytest.approx(182.30) and t["asks"][0][0] == pytest.approx(182.40)
    assert parse_packet(b"\x01\x02short") is None


def test_reversed_book_flags_are_fixed():
    b = build_packet(3, 2, "1", 1, 100.0, bids=[(100.5, 10)], asks=[(99.5, 10)])
    t = parse_packet(b)
    assert t["bids"][0][0] < t["asks"][0][0]


def test_feed_subscription_diff_without_socket():
    f = AngelFeed(None, "k", "c", lambda t, rx: None)
    f.want({1: {"99926000"}, 2: {"a", "b"}})
    assert set(f.subs) == {(1, "99926000"), (2, "a"), (2, "b")}
    f.want({1: {"99926000"}, 2: {"b", "c"}})
    assert set(f.subs) == {(1, "99926000"), (2, "b"), (2, "c")}


# ---------------------------------------------------------------- secrets


def test_secret_never_prints():
    sec = Secret("ABCD1234SECRET")
    assert "ABCD" not in str(sec) and "ABCD" not in repr(sec) and "ABCD" not in f"{sec}"
    assert sec.reveal() == "ABCD1234SECRET"


def test_log_lines_are_redacted(caplog):
    register_secret("MyApiKey123")
    jwt = "eyJhbGciOiJIUzUxMiJ9.eyJ1c2VybmFtZSI6IkExMjMifQ.c2lnbmF0dXJlc2lnbmF0dXJl"
    assert "MyApiKey123" not in redact(f"headers X-PrivateKey: MyApiKey123 Authorization: Bearer {jwt}")
    assert jwt not in redact(f"Bearer {jwt}")
    logger = logging.getLogger("test.redact")
    h = logging.StreamHandler()
    h.addFilter(RedactingFilter())
    rec = logger.makeRecord("t", logging.ERROR, __file__, 1, "key=%s", ("MyApiKey123",), None)
    RedactingFilter().filter(rec)
    assert "MyApiKey123" not in rec.getMessage()


def test_load_secrets(tmp_path):
    p = tmp_path / "secrets.toml"
    p.write_text('[angel]\napi_key = "PASTE_HERE"\nclient_code = "PASTE_HERE"\npin = "PASTE_HERE"\ntotp_secret = "PASTE_HERE"\n')
    assert load_secrets(p) is None
    p.write_text('[angel]\napi_key = "k1"\nclient_code = "a123"\npin = "12x4"\ntotp_secret = "JBSWY3DPEHPK3PXP"\n')
    s = load_secrets(p)
    assert s.client_code.reveal() == "A123"
    assert any("digits" in x for x in s.problems())
    assert "k1" not in repr(s)


def test_settings_roundtrip_and_validation(tmp_path):
    p = tmp_path / "settings.toml"
    s = Settings(max_loss_per_trade=1500, daily_loss_limit=4500, target_r=1.5, stop_rule="far")
    save_settings(s, p)
    back = load_settings(p)
    assert back.max_loss_per_trade == 1500 and back.target_r == 1.5 and back.stop_rule == "far"
    bad = Settings(target_r=3.0)
    assert bad.validate()
    with pytest.raises(ValueError):
        save_settings(bad, p)


def test_no_order_placement_code_anywhere():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    banned = ("placeOrder", "/order/v1/", "modifyOrder", "cancelOrder", "gtt/v1", "convertPosition")
    for f in list((root / "app").glob("*.py")) + [root / "web" / "app.js"]:
        text = f.read_text(encoding="utf-8")
        for b in banned:
            assert b not in text, f"{b} found in {f.name}"


def test_server_binds_localhost_only():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "__main__.py").read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in text and "0.0.0.0" not in text


# ------------------------------------------------------------ instruments


def test_parse_master_rows():
    rows = [
        {"token": "1", "symbol": "NIFTY17NOV2625600CE", "name": "NIFTY", "expiry": "17NOV2026", "strike": "2560000.000000",
         "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
        {"token": "2", "symbol": "NIFTY17NOV2625650PE", "name": "NIFTY", "expiry": "17NOV2026", "strike": "2565000.000000",
         "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
        {"token": "3", "symbol": "SENSEX26N1283800CE", "name": "SENSEX", "expiry": "12NOV2026", "strike": "8380000.000000",
         "lotsize": "20", "instrumenttype": "OPTIDX", "exch_seg": "BFO", "tick_size": "5.000000"},
        {"token": "4", "symbol": "SENSEX5026N12CE", "name": "SENSEX50", "expiry": "12NOV2026", "strike": "2700000",
         "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "BFO", "tick_size": "5"},
        {"token": "5", "symbol": "NIFTY17NOV26FUT", "name": "NIFTY", "expiry": "17NOV2026", "strike": "-1",
         "lotsize": "65", "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "10"},
    ]
    cs = parse_master(rows)
    assert {c.token for c in cs} == {"1", "2", "3"}
    n = [c for c in cs if c.token == "1"][0]
    assert n.strike == 25600 and n.tick == 0.05 and n.lot == 65 and n.expiry == date(2026, 11, 17)
    assert n.expiry_code == "17NOV2026" and n.ws_type == 2
    sx = [c for c in cs if c.token == "3"][0]
    assert sx.ws_type == 4 and sx.lot == 20
    assert OptionUniverse("NIFTY", cs).step(date(2026, 11, 17)) == 50
