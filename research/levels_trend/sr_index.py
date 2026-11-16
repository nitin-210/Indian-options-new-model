"""Support / resistance and liquidity-pool trades on NIFTY (2015-2026) and SENSEX (2018-2026), 5-minute candles.

Levels known before the open:  yesterday's high, low, close; last week's high/low (previous 5 sessions); swing
highs/lows of the last 3 sessions (15-minute pivots, 2 candles each side); floor pivots R1/S1; round numbers
(NIFTY every 100, SENSEX every 500) within 1.5x the average daily range of the open.  Intraday levels (variant
"+intraday"): the opening range high/low from 09:30, and the day's own high/low once it is 30+ minutes old.
A tolerance of 0.05x the average daily range counts as a touch.

Trades (signal on a 5-minute close 09:30 - 14:00, the first of the day per strategy):
  bounce  first touch of a support today, the candle closes back above it and green -> CE (mirror: resistance -> PE);
          stop 0.05x ADR beyond the candle's extreme
  sweep   "liquidity grab": the candle pierces a level by 0.03x ADR or more and closes back on the other side ->
          trade the reversal; stop just beyond the sweep's extreme
  flip    a close through a level, then within 12 candles a pull back that touches it and closes on the breakout side
          -> trade the breakout direction; stop beyond the retest candle
Exits from lab.exits (hold, 1R / 1.5R / 2R targets, trailing, breakeven). Option P&L as lab.opt_pnl.
Also: the opening-range rule with the levels as a filter (skip if a level blocks the way within 0.5R) and as a
target (exit at the first level beyond +1R).
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402

ROUND = {"NIFTY": 100.0, "SENSEX": 500.0}


def pivots15(a_days):
    """swing highs/lows of 15-minute candles over the given days (2 candles each side)"""
    hs, ls = [], []
    for a in a_days:
        h15 = a[:, 1].reshape(-1, 3).max(1); l15 = a[:, 2].reshape(-1, 3).min(1)
        hs.append(h15); ls.append(l15)
    h, l = np.concatenate(hs), np.concatenate(ls)
    ph = [h[k] for k in range(2, len(h) - 2) if h[k] == h[k - 2:k + 3].max()]
    pl = [l[k] for k in range(2, len(l) - 2) if l[k] == l[k - 2:k + 3].min()]
    return ph, pl


def preopen_levels(key, ds, days, fac, j):
    d = ds[j]
    F = fac[d]
    prev = [days[x] for x in ds[max(0, j - 5):j]]
    wk_h, wk_l = max(a[:, 1].max() for a in prev), min(a[:, 2].min() for a in prev)
    P = (F["ph"] + F["pl"] + F["pc"]) / 3
    lv = [F["ph"], F["pl"], F["pc"], wk_h, wk_l, 2 * P - F["pl"], 2 * P - F["ph"]]
    ph, pl = pivots15([days[x] for x in ds[max(0, j - 3):j]])
    lv += ph + pl
    o = days[d][0, 0]
    step = ROUND[key]
    r = np.arange(np.floor((o - 1.5 * F["atr"]) / step) * step, o + 1.5 * F["atr"] + step, step)
    lv += list(r)
    lv = np.array(sorted(set(round(x, 2) for x in lv if abs(x - o) <= 1.5 * F["atr"])))
    return lv


def signal(a, lv, atr, kind, intraday, dyn=None):
    o, h, l, c = a.T
    tol = 0.05 * atr
    touched = np.zeros(len(lv), bool)            # a level touched earlier today (bounce wants the first touch)
    for i in range(0, lab.LAST_SIG + 1):
        L = lv
        if intraday and i >= 3:
            extra = [h[:3].max(), l[:3].min()]
            if i >= 9:                            # day's extremes formed at least 30 minutes ago
                extra += [h[:i - 5].max(), l[:i - 5].min()]
            if dyn is not None:                   # a moving level known at candle i (e.g. VWAP to the previous candle)
                extra += [dyn[i]]
            L = np.concatenate([lv, extra])
        if i >= 3 and i >= 1:
            pc_ = c[i - 1]
            for k, x in enumerate(L):
                sup, res = pc_ > x, pc_ < x
                first = k >= len(lv) or not touched[k]
                if kind == "bounce" and first:
                    if sup and l[i] <= x + tol and l[i] >= x - 0.25 * atr and c[i] > x and c[i] > o[i]:
                        return i, 1, l[i] - tol
                    if res and h[i] >= x - tol and h[i] <= x + 0.25 * atr and c[i] < x and c[i] < o[i]:
                        return i, -1, h[i] + tol
                elif kind == "sweep":
                    if sup and l[i] < x - 0.03 * atr and c[i] > x:
                        return i, 1, l[i] - 0.02 * atr
                    if res and h[i] > x + 0.03 * atr and c[i] < x:
                        return i, -1, h[i] + 0.02 * atr
                elif kind == "flip":
                    for b in range(max(0, i - 12), i):       # a close through x at candle b (from the other side)
                        if b < 1:
                            continue
                        if c[b - 1] < x < c[b] and (c[b] - x) > tol and l[i] <= x + tol and c[i] > x and i > b:
                            if all(c[m] > x for m in range(b, i)):
                                return i, 1, l[i] - tol
                        if c[b - 1] > x > c[b] and (x - c[b]) > tol and h[i] >= x - tol and c[i] < x and i > b:
                            if all(c[m] < x for m in range(b, i)):
                                return i, -1, h[i] + tol
        # remember touches (for "first touch")
        for k, x in enumerate(lv):
            if l[i] <= x + tol and h[i] >= x - tol:
                touched[k] = True
    return None


def app_orb(a, F):
    if F.get("yra") is None or F["yra"] > 1.2 or F.get("ydir") is None:
        return None
    o, h, l, c = a.T
    H, L = h[:3].max(), l[:3].min()
    w = H - L
    if w <= 0:
        return None
    sd = F["ydir"]
    for i in range(3, lab.LAST_SIG + 1):
        if (c[i] > H + 0.2 * w) if sd == 1 else (c[i] < L - 0.2 * w):
            return i, sd, (L if sd == 1 else H)
    return None


def level_target_exit(a, i, sd, stop, lv):
    """exit at the first level at least 1R beyond the entry (else stop / 15:00), stop trails like trail1 after +1R"""
    o, h, l, c = a.T
    e = c[i]
    risk = (e - stop) * sd
    tg = [x for x in lv if (x - e) * sd >= risk]
    tgt = (min(tg) if sd == 1 else max(tg)) if tg else None
    st, best = stop, e
    for j in range(i + 1, lab.I1455 + 1):
        if (l[j] <= st) if sd == 1 else (h[j] >= st):
            return (min(st, o[j]) if sd == 1 else max(st, o[j])), j
        if tgt is not None and ((h[j] >= tgt) if sd == 1 else (l[j] <= tgt)):
            return tgt, j
        best = max(best, h[j]) if sd == 1 else min(best, l[j])
        if (best - e) * sd >= risk:
            st = max(st, best - risk) if sd == 1 else min(st, best + risk)
    return c[lab.I1455], lab.I1455


def main():
    vix = lab.load_vix()
    rows = []
    for key in ("NIFTY", "SENSEX"):
        days, ind, fac = lab.prepare(key)
        ds = sorted(days)
        for j, d in enumerate(ds):
            if d < lab.START[key] or "atr" not in fac[d] or j < 6:
                continue
            a, F = days[d], fac[d]
            lv = preopen_levels(key, ds, days, fac, j)
            vx = vix.get(d)
            exp = odds.weekly_expiry_for(key, d)

            def add(name, s, exits_only=None):
                i, sd, stop = s
                ex, risk = lab.exits(a, i, sd, stop)
                if ex is None:
                    return
                iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                S0 = float(a[i, 3])
                for exn, (px, jj, half) in ex.items():
                    if exits_only and exn not in exits_only:
                        continue
                    if exn == "half1":
                        continue
                    rows.append((key, d, name, exn, sd, S0, (i + 1) * 5, iv, exp, (px - S0) * sd / risk, risk, i,
                                 float(px), (jj + 1) * 5, 1.0))

            for kind in ("bounce", "sweep", "flip"):
                for intra in (False, True):
                    s = signal(a, lv, F["atr"], kind, intra)
                    if s and (a[s[0], 3] - s[2]) * s[1] > 0:
                        add(f"{kind} {'+intraday' if intra else 'pre-open levels'}", s)
            s = app_orb(a, F)
            if s:
                i, sd, stop = s
                add("ORB app rule", s, ("trail1", "hold"))
                risk = (a[i, 3] - stop) * sd
                block = [x for x in lv if 0 < (x - a[i, 3]) * sd < 0.5 * risk]
                if not block:
                    add("ORB, skip if a level is within 0.5R ahead", s, ("trail1",))
                px, jj = level_target_exit(a, i, sd, stop, lv)
                iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                rows.append((key, d, "ORB, exit at the first level beyond 1R", "level", sd, float(a[i, 3]), (i + 1) * 5,
                             iv, exp, (px - a[i, 3]) * sd / risk, risk, i, float(px), (jj + 1) * 5, 1.0))
        print(key, "done", len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS)
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m).astype("float32")
    T.drop(columns=["frac"]).to_pickle("/home/claude/ml/lab/sr_trades.pkl")
    print("saved", T.shape)


if __name__ == "__main__":
    main()
