"""Day-type models (exact match with the research models), star ratings, and the dashboard predictions."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import demo as demomod
from app.daytype import DayTypeModels, rating
from app.predict import Predictor

from test_engine_server import make_demo, run_until

VECTORS = Path(__file__).parent / "daytype_vectors.json"


@pytest.fixture(scope="module")
def models():
    return DayTypeModels()


def test_models_match_research_exactly(models):
    vecs = json.loads(VECTORS.read_text())
    assert len(vecs) >= 100
    for v in vecs:
        p = models.models[v["cp"]][v["target"]].prob(v["x"])
        assert p == pytest.approx(v["p"], abs=1e-9), (v["cp"], v["target"])


def test_checkpoints(models):
    assert models.checkpoint_for("09:20") is None
    assert models.checkpoint_for("09:30") == "09:30"
    assert models.checkpoint_for("10:59") == "10:15"
    assert models.checkpoint_for("15:10") == "14:00"


def test_star_rules():
    big = rating({"big_up": 0.55, "big_dn": 0.05, "small": 0.2}, "CE")
    assert big["stars"] == 3 and big["p_big_with"] == pytest.approx(0.55)
    # a big move the other way does not earn stars for our side
    other = rating({"big_up": 0.55, "big_dn": 0.05, "small": 0.2}, "PE")
    assert other["stars"] == 2
    small = rating({"big_up": 0.05, "big_dn": 0.05, "small": 0.75}, "CE")
    assert small["stars"] == 1
    mid = rating({"big_up": 0.2, "big_dn": 0.1, "small": 0.4}, None)
    assert mid["stars"] == 2
    assert 0 <= mid["p_medium"] <= 1


def test_predictions_before_open_are_pending(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    snap = Predictor(e).snapshot()
    assert set(snap["indices"]) == {"NIFTY", "SENSEX", "BANKNIFTY"}
    for c in snap["indices"].values():
        assert c["plan"] is None
        assert c["rating"] is None or c["rating"].get("pending")
    assert snap["indices"]["BANKNIFTY"]["tested"] is False


def test_predictions_after_signal(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    for i, k in enumerate(("NIFTY", "SENSEX")):   # as the demo app does: the move-size model needs 66+ past sessions
        e.set_history(k, demomod.synthetic_history(k, src.day, 120, 3 + i), demomod.synthetic_vix(src.day, 120, 3), "synthetic")
    pr = Predictor(e)
    run_until(src, "10:20")
    snap = pr.snapshot()
    n = snap["indices"]["NIFTY"]
    p = n["plan"]
    assert p and p["side"] == "CE" and p["signal_time"] == "09:50"
    t = p["targets"]
    assert p["stop"] < p["entry"] < t["T1"] < t["T2"] < t["T3"]
    assert t["T1"] - p["entry"] == pytest.approx(p["risk_pts"], abs=0.02)
    assert p["atm"]["opt"] == "CE" and p["atm"]["money"] in ("ATM", "1 OTM", "1 ITM")
    assert p["otm"]["strike"] > p["atm"]["strike"]
    # option premium estimates rise toward the targets and fall toward the stop
    at = p["atm"]["at"]
    assert at["stop"] < at["entry"] < at["T1"] < at["T2"] < at["T3"]
    assert n["or"]["buf"] == pytest.approx(0.2 * (n["or"]["high"] - n["or"]["low"]), abs=0.02)
    assert n["rating"] and not n["rating"].get("pending") and n["rating"]["stars"] in (1, 2, 3)
    assert any("closed at" in r and "trigger" in r for r in n["reasons"])
    assert p["trigger"] == pytest.approx(n["or"]["high"] + n["or"]["buf"], abs=0.02)
    sx = snap["indices"]["SENSEX"]
    assert sx["plan"] and sx["plan"]["side"] == "PE"
    assert sx["plan"]["stop"] > sx["plan"]["entry"] > sx["plan"]["targets"]["T1"]
    picks = snap["picks"]
    assert 1 <= len(picks) <= 10
    for pk in picks:
        assert pk["stars"] in (1, 2, 3) and pk["tested"] is False
        assert any("reference only" in r for r in pk["reasons"])
        if pk["levels"]:
            lv = pk["levels"]
            sd = 1 if lv["side"] == "CE" else -1
            assert sd * (lv["targets"]["T1"] - lv["entry"]) > 0 and sd * (lv["entry"] - lv["stop"]) > 0
    assert [pk["score"] for pk in picks] == sorted((pk["score"] for pk in picks), reverse=True)


def test_dashboard_is_served(tmp_path):
    from app.server import Runtime, create_app
    s, e, src, j = make_demo(tmp_path)
    rt = Runtime(s, e, src, e.hub, j, 8765)
    c = TestClient(create_app(rt, start_background=False))
    r = c.get("/")
    assert r.status_code == 200 and "dashboard.js" in r.text and "nothing is placed" in r.text
    assert c.get("/classic").status_code == 200
    assert c.get("/static/dashboard.js").status_code == 200
    assert c.get("/static/vendor/fonts/chakra-petch-latin-600-normal.woff2").status_code == 200
    d = c.get("/api/predictions").json()
    assert "indices" in d and "picks" in d and d["settings"]["stop_rule"] in ("mid", "far")


def test_any_opened_stock_gets_a_card(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    pr = Predictor(e)
    run_until(src, "11:00")
    snap = pr.snapshot()
    picked = {p["name"] for p in snap["picks"]}
    others = [n for n, st in e.movers.stocks.items() if n not in picked and st.ltp > 0 and n in e.movers.universes]
    assert others, "the demo should have stocks outside the top 10"
    e.chart_stock = others[0]
    sel = pr.snapshot()["selected"]
    assert sel and sel["name"] == others[0] and sel["in_picks"] is False
    assert sel["stars"] in (1, 2, 3) and sel["or"] is not None
    e.chart_stock = next(iter(picked))
    assert pr.snapshot()["selected"] is None      # a pick already has its card in the list


def test_stock_signals_report_their_outcome(tmp_path):
    s, e, src, j = make_demo(tmp_path)
    pr = Predictor(e)
    run_until(src, "15:10")
    snap = pr.snapshot()
    broke = [p for p in snap["picks"] if p["breakout"]]
    assert broke
    for p in broke:
        oc = p["outcome"]
        assert oc["status"] in ("stopped", "closed", "open")
        if oc["status"] == "stopped":
            assert oc["r"] == -1.0 and any("Stopped out" in r for r in p["reasons"])
        assert any("on the day" in r for r in p["reasons"])
