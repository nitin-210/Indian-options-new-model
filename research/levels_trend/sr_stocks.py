"""Support / resistance, liquidity sweeps and volume levels on 55 large-cap F&O stocks (5-minute, Dec 2022 - Nov 2026).

Same trades as sr_index.py (bounce, sweep, flip; first of the day; 09:30 - 14:00), with stock levels known before the
open: yesterday's high, low, close; last week's high/low; swing highs/lows of the last 3 sessions; and yesterday's
VOLUME PROFILE: point of control (the price with the most volume) and the value area high / low (70% of volume).
Variant "+intraday" adds the opening range high/low, the day's own 30-minute-old extremes and today's VWAP.
Results in R (before costs) and in rupees for an ATM monthly option on about Rs 5 lakh of stock (m1stocks).
"""
import glob
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
sys.path.insert(0, "/home/claude/ml/m1")
sys.path.insert(0, "/home/claude/build/india-options/research")
import lab  # noqa: E402
import sr_index as SR  # noqa: E402
from brk_events import load, profile, value_area  # noqa: E402
from m1stocks import monthly_expiry, stock_opt_pnl  # noqa: E402


def vwap_prev(a):
    o, h, l, c, v = a.T
    vw = np.cumsum((h + l + c) / 3 * np.maximum(v, 1)) / np.cumsum(np.maximum(v, 1))
    return np.concatenate([[vw[0]], vw[:-1]])


def main():
    parts = []
    files = sorted(glob.glob("/home/claude/ml/data5m/stocks5m/*_5m.json"))
    for n_, f in enumerate(files):
        name = f.split("/")[-1].replace("_5m.json", "")
        days = load(f)
        ds = sorted(days)
        rows = []
        closes = np.array([days[d][-1, 3] for d in ds])
        lr = np.diff(np.log(closes), prepend=np.nan)
        for j, d in enumerate(ds):
            if j < 21:
                continue
            a = days[d]
            prev = [days[x] for x in ds[j - 5:j]]
            y = days[ds[j - 1]]
            atr = float(np.mean([days[x][:, 1].max() - days[x][:, 2].min() for x in ds[j - 14:j]]))
            if atr <= 0:
                continue
            ph, pl, pc = y[:, 1].max(), y[:, 2].min(), y[-1, 3]
            lv = [ph, pl, pc, max(p[:, 1].max() for p in prev), min(p[:, 2].min() for p in prev)]
            sh, sl = SR.pivots15([days[x][:, :4] for x in ds[j - 3:j]])
            lv += sh + sl
            edges, vol = profile(*y.T, pl, ph)
            va = value_area(edges, vol)
            if va:
                lv += list(va)
            o0 = a[0, 0]
            lv = np.array(sorted(set(round(x, 2) for x in lv if abs(x - o0) <= 1.5 * atr)))
            iv = max(0.15, 1.1 * np.nanstd(lr[j - 20:j]) * np.sqrt(252))
            exp = monthly_expiry(d)
            for kind in ("bounce", "sweep", "flip"):
                for intra in (False, True):
                    s = SR.signal(a[:, :4], lv, atr, kind, intra, dyn=vwap_prev(a) if intra else None)
                    if not s:
                        continue
                    i, sd, stop = s
                    if (a[i, 3] - stop) * sd <= 0:
                        continue
                    ex, risk = lab.exits(a[:, :4], i, sd, stop)
                    if ex is None:
                        continue
                    for exn in ("hold", "t1", "t15", "trail1", "be1"):
                        px, jj, _ = ex[exn]
                        rows.append((name, d, f"{kind} {'+intraday' if intra else 'pre-open levels'}", exn, sd,
                                     float(a[i, 3]), (i + 1) * 5, iv, exp, (px - a[i, 3]) * sd / risk, risk, i,
                                     float(px), (jj + 1) * 5, 1.0))
        T = pd.DataFrame(rows, columns=lab.COLS)
        if len(T):
            T["pnl_cal"] = stock_opt_pnl(T, "cal").astype("float32")
            T["pnl_mkt"] = stock_opt_pnl(T, "mkt").astype("float32")
            parts.append(T[["key", "date", "strat", "exit", "sd", "R", "risk", "pnl_cal", "pnl_mkt"]])
        print(f"{n_ + 1}/{len(files)} {name} {len(T)}", flush=True)
    T = pd.concat(parts, ignore_index=True)
    T.to_pickle("/home/claude/ml/lab/sr_stock_trades.pkl")
    print("saved", T.shape)


if __name__ == "__main__":
    main()
