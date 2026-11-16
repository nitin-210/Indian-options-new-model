"""Stock range-break events (the app's rule) with features known at the signal, and the outcome.

Rule (same as the dashboard's Range breaks / picks): opening range = 09:15-09:30 (three 5-min candles); a break is the
first 5-min candle starting 09:30 or later and closing by 14:00 whose close is beyond the range by 0.2x its width.
Entry = that close. Stop = far side of the range. Exit = stop, or the 14:55-15:00 candle's close (15:00).
Outcome in R (underlying): (exit - entry) * side / (entry - stop distance).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

D = Path("/home/claude/ml/data5m")
SLOTS = 75                                   # 09:15 .. 15:25
T930, T1355, T1455 = 3, 56, 56 + 12          # slot of 09:30, last signal-candle start 13:55, 14:55


def load(path):
    rows = json.loads(Path(path).read_text())
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.drop_duplicates("ts").sort_values("ts")
    m = df["ts"].dt.hour * 60 + df["ts"].dt.minute
    df = df[(m >= 555) & (m <= 925) & (m % 5 == 0)].copy()
    df["slot"] = ((df["ts"].dt.hour * 60 + df["ts"].dt.minute) - 555) // 5
    df["date"] = df["ts"].dt.date
    days = {}
    for d, g in df.groupby("date", sort=True):
        if len(g) < 70 or g["slot"].iloc[0] != 0:
            continue                          # short / special sessions (Muhurat) and gappy days
        a = np.full((SLOTS, 5), np.nan)
        a[g["slot"].to_numpy(), :] = g[["o", "h", "l", "c", "v"]].to_numpy(float)
        # fill a rare missing slot with the previous close (no volume)
        for i in range(1, SLOTS):
            if np.isnan(a[i, 3]):
                a[i, :4] = a[i - 1, 3]
                a[i, 4] = 0.0
        days[d] = a
    return days


def profile(o, h, l, c, v, lo, hi, nb=40):
    """Volume spread evenly across each candle's high-low, on nb price bins between lo and hi."""
    edges = np.linspace(lo, hi, nb + 1)
    out = np.zeros(nb)
    for H, L, V in zip(h, l, v):
        if V <= 0 or not np.isfinite(H):
            continue
        a = np.clip(np.searchsorted(edges, L, "right") - 1, 0, nb - 1)
        b = np.clip(np.searchsorted(edges, H, "right") - 1, 0, nb - 1)
        out[a:b + 1] += V / (b - a + 1)
    return edges, out


def value_area(edges, vol, share=0.7):
    if vol.sum() <= 0:
        return None
    poc = int(vol.argmax())
    lo = hi = poc
    tot, need = vol[poc], share * vol.sum()
    while tot < need and (lo > 0 or hi < len(vol) - 1):
        up = vol[hi + 1] if hi < len(vol) - 1 else -1
        dn = vol[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1; tot += up
        else:
            lo -= 1; tot += dn
    mid = (edges[:-1] + edges[1:]) / 2
    return mid[poc], edges[lo], edges[hi + 1]


def events_for(name, days, nifty):
    ds = sorted(days)
    out = []
    for i, d in enumerate(ds):
        if i < 21:
            continue
        a = days[d]
        o, h, l, c, v = a.T
        prev = [days[x] for x in ds[i - 20:i]]
        y = days[ds[i - 1]]
        rng = [p[:, 1].max() - p[:, 2].min() for p in prev[-14:]]
        atr = float(np.mean(rng))
        if atr <= 0:
            continue
        orh, orl = h[:3].max(), l[:3].min()
        w = orh - orl
        if w <= 0:
            continue
        up, dn = orh + 0.2 * w, orl - 0.2 * w
        k = side = None
        for j in range(T930, T1355 + 1):
            if c[j] > up:
                k, side = j, 1
                break
            if c[j] < dn:
                k, side = j, -1
                break
        if k is None:
            continue
        entry = c[k]
        stop = orl if side == 1 else orh
        risk = abs(entry - stop)
        exit_px, how, mfe = None, "time", 0.0
        for j in range(k + 1, T1455 + 1):
            if (l[j] <= stop) if side == 1 else (h[j] >= stop):
                exit_px, how = stop, "stop"
                break
            mfe = max(mfe, ((h[j] - entry) if side == 1 else (entry - l[j])) / risk)
        if exit_px is None:
            exit_px = c[T1455]
        R = (exit_px - entry) * side / risk
        tp = {}
        for tgt in (1.0, 1.5, 2.0):
            res = None
            for j in range(k + 1, T1455 + 1):
                if (l[j] <= stop) if side == 1 else (h[j] >= stop):
                    res = -1.0; break
                if ((h[j] - entry) if side == 1 else (entry - l[j])) / risk >= tgt:
                    res = tgt; break
            tp[f"R_tp{tgt:g}"] = res if res is not None else (c[T1455] - entry) * side / risk
        # ---------- features known when the break candle closes (slot k)
        pv = np.array([p[:, 4] for p in prev])                    # 20 x 75 volumes
        cum_prev = pv[:, :k + 1].sum(1)
        f = {}
        f["rvol_brk"] = v[k] / max(np.median(pv[:, k]), 1)
        f["rvol_day"] = v[:k + 1].sum() / max(np.median(cum_prev), 1)
        f["rvol_or"] = v[:3].sum() / max(np.median(pv[:, :3].sum(1)), 1)
        f["rvol_last3"] = v[k - 2:k + 1].sum() / max(np.median(pv[:, k - 2:k + 1].sum(1)), 1)
        turn = np.mean([(p[:, 3] * p[:, 4]).sum() for p in prev]) / 1e7          # avg daily turnover, Rs crore
        f["turnover_cr"] = turn
        f["or_atr"] = w / atr
        f["risk_atr"] = risk / atr
        f["ext"] = (entry - (up if side == 1 else dn)) * side / w              # how far past the trigger it closed
        body = (c[k] - o[k]) * side / max(h[k] - l[k], 1e-9)
        f["body"] = body
        f["gap_atr"] = (o[0] - y[-1, 3]) * side / atr                          # gap in the break's direction
        f["dayret_atr"] = (entry - y[-1, 3]) * side / atr                      # move on the day so far, its direction
        f["yday_dir"] = np.sign(y[-1, 3] - y[0, 0]) * side
        f["yday_ra"] = (y[:, 1].max() - y[:, 2].min()) / atr
        f["vs_pdh"] = ((entry - y[:, 1].max()) if side == 1 else (y[:, 2].min() - entry)) / atr  # beyond y'day's high/low
        # session volume profile so far (09:15 .. k)
        lo_s, hi_s = l[:k + 1].min(), h[:k + 1].max()
        ed, pf = profile(o[:k + 1], h[:k + 1], l[:k + 1], c[:k + 1], v[:k + 1], lo_s, hi_s)
        va = value_area(ed, pf)
        if va:
            poc, val, vah = va
            f["poc_or"] = ((poc - orl) / w) if side == 1 else ((orh - poc) / w)      # 1 = POC at the break side
            f["beyond_va"] = ((entry - vah) if side == 1 else (val - entry)) / w
        # previous 5 sessions' composite profile: how much volume traded in the 1R zone ahead (thin air = low)
        p5 = prev[-5:]
        allh = np.concatenate([p[:, 1] for p in p5]); alll = np.concatenate([p[:, 2] for p in p5])
        allv = np.concatenate([p[:, 4] for p in p5])
        lo5, hi5 = min(alll.min(), entry - 2 * risk), max(allh.max(), entry + 2 * risk)
        ed5, pf5 = profile(None, allh, alll, None, allv, lo5, hi5, nb=60)
        mid5 = (ed5[:-1] + ed5[1:]) / 2
        ahead = (mid5 > entry) & (mid5 <= entry + risk) if side == 1 else (mid5 < entry) & (mid5 >= entry - risk)
        inside = (mid5 >= alll.min()) & (mid5 <= allh.max())
        dens = pf5[inside].mean() if inside.any() else np.nan
        f["ahead_vol"] = pf5[ahead].mean() / dens if ahead.any() and dens > 0 else np.nan
        va5 = value_area(ed5, pf5)
        if va5:
            f["beyond_va5"] = ((entry - va5[2]) if side == 1 else (va5[1] - entry)) / atr
        # market: NIFTY's move from its open to the break, in the break's direction
        nd = nifty.get(d)
        if nd is not None:
            f["nifty_ret"] = (nd[k, 3] / nd[0, 0] - 1) * 100 * side
            f["rs"] = ((entry / o[0] - 1) - (nd[k, 3] / nd[0, 0] - 1)) * 100 * side      # stock vs NIFTY since open
        out.append(dict(name=name, date=d, side=side, slot=k, entry=entry, risk=risk, atr=atr, R=R, how=how,
                        mfe=mfe, **tp, **f))
    return out


if __name__ == "__main__":
    nifty = load(D / "NIFTY_5m.json")
    allev = []
    for p in sorted((D / "stocks5m").glob("*_5m.json")):
        name = p.stem.replace("_5m", "")
        ev = events_for(name, load(p), nifty)
        allev += ev
        print(name, len(ev), file=sys.stderr)
    df = pd.DataFrame(allev)
    df.to_pickle("/home/claude/ml/v2/brk_events.pkl")
    print(df.shape, df["date"].min(), df["date"].max())
    print(df[["R", "mfe"]].describe())
