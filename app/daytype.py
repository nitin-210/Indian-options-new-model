"""Day-type rating (big / medium / small move) for NIFTY, SENSEX and NIFTY BANK, from the research models.

The models (app/models/daytype.json) were trained in the research project on NIFTY + SENSEX 5-minute bars,
2012 - Nov 2026: at each check time they give P(big up day), P(big down day) and P(small day), where big = the move
from the open to 15:00 is >= 150 NIFTY / 500 SENSEX points and small < 75 / 250. Walk-forward accuracy on years the
models had not seen: AUC about 0.70 at 10:00 rising to 0.87 at 13:00 (0.5 = coin flip). NIFTY BANK has no model of
its own: the NIFTY model is used with a scaled point threshold, so its rating is approximate.

Everything here uses only completed 5-minute candles and data known before the open. Pure Python (no lightgbm).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent / "models" / "daytype.json"


class TreeModel:
    """Evaluates the exported LightGBM trees (binary objective: sigmoid of the summed leaf values)."""

    def __init__(self, trees: list[dict]):
        self.trees = trees

    def raw(self, x: list[float | None]) -> float:
        total = 0.0
        for t in self.trees:
            n = t["root"]
            f, thr, left, right, dl, miss = t["f"], t["t"], t["l"], t["r"], t["d"], t["m"]
            while n >= 0:
                v = x[f[n]]
                isnan = v is None or (isinstance(v, float) and math.isnan(v))
                m = miss[n]
                if m == 2 and isnan:                       # missing = NaN: follow the default side
                    go_left = bool(dl[n])
                elif m == 1 and (isnan or abs(v) <= 1e-35):  # missing = zero
                    go_left = bool(dl[n])
                else:
                    vv = 0.0 if isnan else float(v)
                    go_left = vv <= thr[n]
                n = left[n] if go_left else right[n]
            total += t["v"][~n]
        return total

    def prob(self, x: list[float | None]) -> float:
        z = self.raw(x)
        return 1.0 / (1.0 + math.exp(-z)) if z > -700 else 0.0


class DayTypeModels:
    def __init__(self, path: Path = MODEL_PATH):
        d = json.loads(path.read_text(encoding="utf-8"))
        self.meta = d["meta"]
        self.features: list[str] = self.meta["features"]
        self.checkpoints: list[str] = self.meta["checkpoints"]
        self.models = {cp: {tg: TreeModel(trees) for tg, trees in m.items()} for cp, m in d["models"].items()}

    def checkpoint_for(self, hhmm: str) -> str | None:
        """The latest check time at or before hhmm (None before the first one)."""
        ok = [c for c in self.checkpoints if c <= hhmm]
        return ok[-1] if ok else None

    def predict(self, cp: str, feats: dict) -> dict:
        x = [feats.get(name) for name in self.features]
        return {tg: m.prob(x) for tg, m in self.models[cp].items()}


@dataclass
class DailyContext:
    """Yesterday-based values, known before today's open (same definitions as the research features)."""
    prev_close: float
    prev_high: float
    prev_low: float
    y_ret: float
    y_range_atr: float
    cpr_w: float
    ma65_dist: float
    mom5: float
    atr14_pct: float


def daily_context(days: list[tuple[date, float, float, float, float]]) -> DailyContext | None:
    """days = [(date, open, high, low, close), ...] for COMPLETED sessions before today, oldest first."""
    if len(days) < 66:
        return None
    hs = [d[2] for d in days]
    ls = [d[3] for d in days]
    cs = [d[4] for d in days]
    rng = [h - l for h, l in zip(hs, ls)]
    atr14 = sum(rng[-14:]) / 14
    h, l, c = hs[-1], ls[-1], cs[-1]
    P = (h + l + c) / 3
    bc = (h + l) / 2
    return DailyContext(
        prev_close=c, prev_high=h, prev_low=l,
        y_ret=math.log(c / cs[-2]), y_range_atr=(h - l) / atr14 if atr14 else float("nan"),
        cpr_w=abs(2 * P - 2 * bc) / P, ma65_dist=c / (sum(cs[-65:]) / 65) - 1, mom5=c / cs[-6] - 1,
        atr14_pct=atr14 / c)


def live_features(bars: list, ctx: DailyContext, *, is_nifty: float, big_pts: float, vix: float | None,
                  vix_open: float | None, breadth: float | None, breadth_1h: float | None,
                  other_ret_open: float | None, dte: int, weekday: int) -> dict | None:
    """bars = today's COMPLETED 5-minute candles from 09:15 (objects with .o .h .l .c). Mirrors daytype2.build()."""
    if not bars or ctx is None or not (ctx.atr14_pct > 0):
        return None
    o = bars[0].o
    atr = ctx.atr14_pct * o
    c = [b.c for b in bars]
    h = [b.h for b in bars]
    lo = [b.l for b in bars]
    ct = c[-1]
    hi_, lo_ = max(h), min(lo)
    tp = [(b.h + b.l + b.c) / 3 for b in bars]
    tw, s = [], 0.0
    for i, v in enumerate(tp):
        s += v
        tw.append(s / (i + 1))
    side = [1 if ci > ti else (-1 if ci < ti else 0) for ci, ti in zip(c, tw)]
    nz = [x for x in side if x != 0]
    crosses = sum(1 for a, b in zip(nz, nz[1:]) if a != b)
    path = sum(abs(b - a) for a, b in zip([o] + c[:-1], c))
    run_h, run_l, newh, newl = h[0], lo[0], 0, 0
    for i in range(len(bars)):
        prev_h = h[0] if i == 0 else run_h
        prev_l = lo[0] if i == 0 else run_l
        if h[i] > prev_h:
            newh += 1
        if lo[i] < prev_l:
            newl += 1
        run_h, run_l = max(run_h, h[i]), min(run_l, lo[i])
    orh, orl = max(h[:3]), min(lo[:3])
    after = c[3:]
    up_break = any(x > orh for x in after)
    dn_break = any(x < orl for x in after)
    rets = [math.log(b / a) for a, b in zip(c, c[1:])] if len(c) > 2 else []
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))      # population std, as numpy's default
    else:
        sd = float("nan")
    nan = float("nan")
    return {
        "f_sofar": (ct - o) / atr, "f_abs_sofar": abs(ct - o) / atr, "f_hi": (hi_ - o) / atr, "f_lo": (o - lo_) / atr,
        "f_range": (hi_ - lo_) / atr, "f_pos": (ct - lo_) / max(hi_ - lo_, 1e-9), "f_eff": abs(ct - o) / max(path, 1e-9),
        "f_twap": (ct - tw[-1]) / atr, "f_above_twap": sum(1 for x in side if x > 0) / len(side),
        "f_twap_cross": crosses, "f_newh": newh, "f_newl": newl, "f_newhl": newh - newl,
        "f_m30": (ct - c[-7]) / atr if len(c) > 7 else 0.0, "f_m60": (ct - c[-13]) / atr if len(c) > 13 else 0.0,
        "f_rvol": sd * math.sqrt(75) / ctx.atr14_pct if len(rets) > 2 else nan,
        "f_or_w": (orh - orl) / atr, "f_or_state": 1.0 if ct > orh else (-1.0 if ct < orl else 0.0),
        "f_or_both": float(up_break and dn_break), "f_gap": (o - ctx.prev_close) / atr,
        "f_gap_filled": float(lo_ <= ctx.prev_close <= hi_), "f_pdh": (ct - ctx.prev_high) / atr,
        "f_pdl": (ct - ctx.prev_low) / atr,
        "f_br": (breadth - 0.5) if breadth is not None else nan,
        "f_br_chg": (breadth - breadth_1h) if breadth is not None and breadth_1h is not None else nan,
        "f_other": other_ret_open / ctx.atr14_pct if other_ret_open is not None else nan,
        "f_vix": vix if vix else nan,
        "f_vix_chg": math.log(vix / vix_open) if vix and vix_open else nan,
        "f_yr": ctx.y_range_atr, "f_yret": ctx.y_ret, "f_ma65": ctx.ma65_dist, "f_mom5": ctx.mom5, "f_cpr": ctx.cpr_w,
        "f_dte": dte, "f_wd": weekday, "f_atr_pts_ratio": big_pts / atr, "f_is_nifty": is_nifty,
    }


def rating(p: dict, side: str | None) -> dict:
    """Turn the three probabilities into stars and words.
    Thresholds from the walk-forward test: P(big our way) >= 0.5 at 11:00 was right ~59% (69% at 12:30);
    P(small) >= 0.7 was really small ~71-78% of the time (and only 3-6% of those days ended big)."""
    up, dn, sm = p["big_up"], p["big_dn"], p["small"]
    if side == "CE":
        with_ = up
    elif side == "PE":
        with_ = dn
    else:
        with_ = max(up, dn)
    med = max(0.0, 1.0 - up - dn - sm)
    if with_ >= 0.5:
        stars, label = 3, "BIG move likely"
    elif sm >= 0.7:
        stars, label = 1, "Small / range day likely"
    elif with_ >= 0.3:
        stars, label = 2, "Could extend (medium to big)"
    else:
        stars, label = 2, "Normal / medium day"
    lean = "up" if up > dn else "down"
    return {"stars": stars, "label": label, "p_big_with": round(with_, 3), "p_big_up": round(up, 3),
            "p_big_dn": round(dn, 3), "p_small": round(sm, 3), "p_medium": round(med, 3), "lean": lean}
