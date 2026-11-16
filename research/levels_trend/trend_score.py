"""Can we tell, early in the day, whether a stock will trend (and which way) or go sideways?

55 large-cap F&O stocks, 5-minute candles with volume, Dec 2022 - Nov 2026. For each stock-day, features known at
09:30 (after the first 15 minutes) and at 10:15 (after the first hour):
  range so far / normal daily range (ATR14), opening gap / ATR (signed), move since the open / ATR (signed),
  relative volume (volume so far / its 20-day average for the same minutes), strength against NIFTY (stock's % move
  since the open minus NIFTY's), position against today's VWAP, yesterday NR7 (narrowest of 7 days), volatility
  squeeze (20-day Bollinger width percentile over 120 days), 20-day trend (close vs 20-day average, in ATR),
  yesterday's candle direction and close location.
Labels from the decision time to 15:00:  trend = the close at 15:00 is at least 0.5 ATR away from the price at the
decision time (else sideways); direction = sign of that move.
Model: gradient boosting, trained on the first 60% of dates, scored on the last 40%. Compared with the current
picks score (first-15-minute range + half the gap, against ATR).
Trading check: at the decision time buy the predicted direction (ATM monthly option on about Rs 5 lakh of stock),
stop at the far side of the range so far, hold to 15:00; and range breakouts taken only in the predicted direction.
"""
import glob
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

sys.path.insert(0, "/home/claude/ml/lab")
sys.path.insert(0, "/home/claude/ml/m1")
sys.path.insert(0, "/home/claude/build/india-options/research")
import lab  # noqa: E402
from brk_events import load  # noqa: E402
from m1stocks import monthly_expiry, stock_opt_pnl  # noqa: E402

I1500 = 68


def build():
    nif = load("/home/claude/ml/data5m/NIFTY_5m.json")
    out = []
    for f in sorted(glob.glob("/home/claude/ml/data5m/stocks5m/*_5m.json")):
        name = f.split("/")[-1].replace("_5m.json", "")
        days = load(f)
        ds = sorted(days)
        closes = np.array([days[d][-1, 3] for d in ds])
        lr = np.diff(np.log(closes), prepend=np.nan)
        for j, d in enumerate(ds):
            if j < 121 or d not in nif:
                continue
            a, y = days[d], days[ds[j - 1]]
            o, h, l, c, v = a.T
            n = nif[d]
            rng = np.array([days[x][:, 1].max() - days[x][:, 2].min() for x in ds[j - 14:j]])
            atr = rng.mean()
            if atr <= 0 or not np.isfinite(c[I1500]):
                continue
            rng7 = [days[x][:, 1].max() - days[x][:, 2].min() for x in ds[j - 7:j]]
            cl = closes[j - 120:j]
            bbw = pd.Series(cl).rolling(20).std().to_numpy() / pd.Series(cl).rolling(20).mean().to_numpy()
            sq = (bbw[-1] <= np.nanpercentile(bbw[19:], 20)) if np.isfinite(bbw[-1]) else False
            yo, yc, yh, yl = y[0, 0], y[-1, 3], y[:, 1].max(), y[:, 2].min()
            base = {"name": name, "date": d, "atr": atr, "gap": (o[0] - yc) / atr, "nr7": float(rng7[-1] <= min(rng7)),
                    "squeeze": float(sq), "trend20": (yc - cl[-20:].mean()) / atr, "ydir": float(np.sign(yc - yo)),
                    "yloc": (yc - yl) / (yh - yl) if yh > yl else 0.5,
                    "iv": max(0.15, 1.1 * np.nanstd(lr[j - 20:j]) * np.sqrt(252)), "exp": monthly_expiry(d)}
            for tag, k in (("0930", 3), ("1015", 12)):
                vol_avg = np.mean([days[x][:k, 4].sum() for x in ds[j - 20:j]])
                vw = np.sum((h[:k] + l[:k] + c[:k]) / 3 * v[:k]) / max(v[:k].sum(), 1)
                p0 = c[k - 1]
                hi, lo = h[:k].max(), l[:k].min()
                fut = c[I1500] - p0
                row = dict(base, t=tag, k=k, price=p0, hi=hi, lo=lo,
                           rng=(hi - lo) / atr, move=(p0 - o[0]) / atr, rvol=v[:k].sum() / vol_avg if vol_avg > 0 else 1.0,
                           rs=(p0 / o[0] - n[k - 1, 3] / n[0, 0]) * 100, vwap=(p0 - vw) / atr,
                           picks=(hi - lo) / atr + 0.5 * abs(o[0] - yc) / atr,
                           fut=fut / atr, trend=float(abs(fut) >= 0.5 * atr), up=float(fut > 0),
                           S1=c[I1500])
                # a trade at the decision time either way: stop at the far side of the range so far, stop or 15:00
                for sd, tagx in ((1, "up"), (-1, "dn")):
                    stop = lo if sd == 1 else hi
                    ex, risk = lab.exits(a[:, :4], k - 1, sd, stop)
                    if ex:
                        px, jj, _ = ex["hold"]
                        row.update({f"{tagx}_S1": px, f"{tagx}_m1": (jj + 1) * 5, f"{tagx}_R": (px - p0) * sd / risk})
                # first range break after the decision time (the app's rule, either side) for the filter test
                H, L = h[:3].max(), l[:3].min(); w = H - L
                brk = None
                for i in range(max(3, k), lab.LAST_SIG + 1):
                    if w > 0 and (c[i] > H + 0.2 * w or c[i] < L - 0.2 * w):
                        brk = i
                        break
                row["brk_i"] = brk
                row["brk_sd"] = (1 if c[brk] > H + 0.2 * w else -1) if brk is not None else 0
                if brk is not None:
                    sd = row["brk_sd"]
                    ex, risk = lab.exits(a[:, :4], brk, sd, L if sd == 1 else H)
                    if ex:
                        px, jj, _ = ex["hold"]
                        row.update(brk_S0=c[brk], brk_S1=px, brk_m0=(brk + 1) * 5, brk_m1=(jj + 1) * 5,
                                   brk_R=(px - c[brk]) * sd / risk)
                out.append(row)
        print(name, len(out), flush=True)
    D = pd.DataFrame(out)
    D.to_pickle("/home/claude/ml/lab/trend_feats.pkl")
    return D


FEATS = ["rng", "move", "gap", "rvol", "rs", "vwap", "nr7", "squeeze", "trend20", "ydir", "yloc"]


def main():
    try:
        D = pd.read_pickle("/home/claude/ml/lab/trend_feats.pkl")
    except FileNotFoundError:
        D = build()
    alld = sorted(D.date.unique())
    cut = alld[int(len(alld) * 0.6)]
    print(f"\n{D.name.nunique()} stocks, {len(alld)} days ({alld[0]} -> {alld[-1]}); train before {cut}")
    for tag in ("0930", "1015"):
        X = D[D.t == tag].copy()
        tr, te = X[X.date < cut], X[X.date >= cut]
        print(f"\n===== decision at {tag[:2]}:{tag[2:]}  (trend days: {te.trend.mean():.0%} of test stock-days)")
        m = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0)
        m.fit(tr[FEATS], tr.trend)
        p = m.predict_proba(te[FEATS])[:, 1]
        print(f"trend vs sideways AUC: model {roc_auc_score(te.trend, p):.3f} | picks score alone "
              f"{roc_auc_score(te.trend, te.picks):.3f} | relative volume alone {roc_auc_score(te.trend, te.rvol):.3f}")
        q = np.quantile(p, 0.9)
        print(f"  top 10% by model: trend {te.trend[p >= q].mean():.0%}; top 10% by picks score: "
              f"{te.trend[te.picks >= te.picks.quantile(0.9)].mean():.0%}; all: {te.trend.mean():.0%}")
        md = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0)
        md.fit(tr[FEATS], tr.up)
        pu = md.predict_proba(te[FEATS])[:, 1]
        print(f"direction AUC (up vs down): model {roc_auc_score(te.up, pu):.3f} | move so far alone "
              f"{roc_auc_score(te.up, te.move):.3f} | strength vs NIFTY alone {roc_auc_score(te.up, te.rs):.3f} (0.5 = coin flip)")
        conf = np.abs(pu - 0.5)
        for share in (0.2, 0.05):
            sel = conf >= np.quantile(conf, 1 - share)
            acc = np.mean((pu[sel] > 0.5) == (te.up.to_numpy()[sel] == 1))
            print(f"  most confident {share:.0%}: direction right {acc:.0%}")
        # trading: buy the predicted direction at the decision time on the most confident stock-days
        te = te.assign(p_trend=p, p_up=pu)
        for lbl, sel in (("confident direction (top 10%)", conf >= np.quantile(conf, 0.9)),
                         ("confident direction AND likely trend", (conf >= np.quantile(conf, 0.8)) & (p >= np.quantile(p, 0.8)))):
            x = te[sel].copy()
            x["sd"] = np.where(x.p_up > 0.5, 1, -1)
            up = x.sd == 1
            x["R_"] = np.where(up, x.up_R, x.dn_R); x["S1_"] = np.where(up, x.up_S1, x.dn_S1); x["m1_"] = np.where(up, x.up_m1, x.dn_m1)
            x = x.dropna(subset=["R_"])
            R = x.R_
            T = pd.DataFrame({"date": x.date.to_numpy(), "sd": x.sd.to_numpy(), "S0": x.price.to_numpy(), "S1": x.S1_.to_numpy(),
                              "m0": x.k.to_numpy() * 5, "m1": x.m1_.to_numpy(), "iv": x.iv.to_numpy(), "exp": x.exp.to_numpy()})
            rs = stock_opt_pnl(T)
            print(f"  trade {lbl}: {len(x) / len(alld[int(len(alld) * .6):]):.1f}/day, direction right {np.mean(R > 0):.0%}, "
                  f"R (stop or 15:00) {R.mean():+.3f}, option ₹/trade {rs.mean():+,.0f}")
        # breakouts only in the predicted direction
        b = te[te.brk_sd != 0].dropna(subset=["brk_R"]).copy()
        agree = np.sign(b.p_up - 0.5) == b.brk_sd
        for lbl, x in (("all range breaks after the decision", b), ("breaks in the PREDICTED direction", b[agree]),
                       ("breaks against the prediction", b[~agree])):
            T = pd.DataFrame({"date": x.date.to_numpy(), "sd": x.brk_sd.to_numpy(), "S0": x.brk_S0.to_numpy(), "S1": x.brk_S1.to_numpy(),
                              "m0": x.brk_m0.to_numpy(), "m1": x.brk_m1.to_numpy(), "iv": x.iv.to_numpy(), "exp": x.exp.to_numpy()})
            rs = stock_opt_pnl(T)
            print(f"  {lbl:<40} n={len(x):>6}  R {x.brk_R.mean():+.3f}  option ₹/trade {rs.mean():+,.0f}")
        imp = pd.Series(np.abs(np.corrcoef(np.c_[tr[FEATS].to_numpy(), tr.trend.to_numpy()].T)[-1, :-1]), FEATS)
        print("  strongest single links to trend days:", imp.sort_values(ascending=False).head(4).round(3).to_dict())


if __name__ == "__main__":
    main()
