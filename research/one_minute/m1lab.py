"""1-minute lab: our tested rules executed on 1-minute candles, and multi-timeframe 1-minute strategies.

Data: NIFTY 1m 2011 - Feb 2023, SENSEX 1m Mar 2018 - Mar 2024 (load1m.py). One trade per strategy per index per day.
Entry at the close of a 1-minute candle; stops checked on every later 1-minute candle (filled at the stop, or at the
open if it gapped through); exit at 15:00 at the latest. Exits for every strategy:
  hold    stop or 15:00
  t2      2R target, else stop or 15:00
  trail1  after +1R (1-minute highs) the stop trails 1R behind the best 1-minute high / low
  trail5  the app's trailing exit: after +1R on a CLOSED 5-minute candle, 1R behind the best closed 5-minute candle
Option P&L as the research lab (lab.opt_pnl): 1 lot ATM, Black-Scholes with India VIX, weekly expiry, 0.5% slippage
each way + charges; "cal" = the app's time model, "mkt" = all decay in market hours (stress).
"""
import sys
from datetime import date

import numpy as np
import pandas as pd
from numba import njit

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402

D = "/home/claude/ml/m1/data"
END = 344                 # the 14:59 minute: exit at its close (15:00)
LAST = 284                # the 13:59 minute: last entry at 14:00


@njit(cache=True)
def ema_nb(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


@njit(cache=True)
def exits_nb(o, h, l, c, i, sd, stop):
    """-> (4, 2): (exit price, exit minute) for hold, t2, trail1, trail5"""
    e = c[i]
    risk = (e - stop) * sd
    res = np.empty((4, 2))
    for x in range(4):
        st = stop
        best = e
        best5 = e
        hi5 = -1e18 if sd == 1 else 1e18
        done = False
        for j in range(i + 1, END + 1):
            if (sd == 1 and l[j] <= st) or (sd == -1 and h[j] >= st):
                res[x, 0] = min(st, o[j]) if sd == 1 else max(st, o[j])
                res[x, 1] = j
                done = True
                break
            if x == 1:
                if (sd == 1 and h[j] - e >= 2 * risk) or (sd == -1 and e - l[j] >= 2 * risk):
                    res[x, 0] = e + sd * 2 * risk
                    res[x, 1] = j
                    done = True
                    break
            elif x == 2:
                best = max(best, h[j]) if sd == 1 else min(best, l[j])
                if (best - e) * sd >= risk:
                    st = max(st, best - risk) if sd == 1 else min(st, best + risk)
            elif x == 3:
                hi5 = max(hi5, h[j]) if sd == 1 else min(hi5, l[j])
                if (j + 1) % 5 == 0:                       # a 5-minute candle closed
                    best5 = max(best5, hi5) if sd == 1 else min(best5, hi5)
                    hi5 = -1e18 if sd == 1 else 1e18
                    if (best5 - e) * sd >= risk:
                        st = max(st, best5 - risk) if sd == 1 else min(st, best5 + risk)
        if not done:
            res[x, 0] = c[END]
            res[x, 1] = END
    return res


def prepare(key):
    z = np.load(f"{D}/{key}.npz")
    dates = [date.fromisoformat(s) for s in z["dates"]]
    B = z["bars"].astype(np.float64)                      # (N, 375, 5)
    N = len(dates)
    o, h, l, c = B[:, :, 0], B[:, :, 1], B[:, :, 2], B[:, :, 3]
    dh, dl, do_, dc = h.max(1), l.min(1), o[:, 0], c[:, -1]
    rng = dh - dl
    fac = []
    for k in range(N):
        f = {}
        if k >= 15:
            atr = rng[k - 14:k].mean()
            f = {"ydir": 1 if dc[k - 1] > do_[k - 1] else -1, "yra": rng[k - 1] / atr, "atr": atr,
                 "ph": dh[k - 1], "pl": dl[k - 1]}
        fac.append(f)
    # 15-minute candles, continuous across days, for the Supertrend (known at each 15-minute close)
    h15 = h.reshape(N, 25, 15).max(2).ravel(); l15 = l.reshape(N, 25, 15).min(2).ravel()
    c15 = c.reshape(N, 25, 15)[:, :, -1].ravel()
    st15, dir15 = lab.supertrend(h15, l15, c15)
    st15, dir15 = st15.reshape(N, 25), dir15.reshape(N, 25)
    # 5-minute EMA20 (known at each 5-minute close), 1-minute EMAs and ATR, continuous across days
    c5 = c.reshape(N, 75, 5)[:, :, -1].ravel()
    e5 = ema_nb(c5, 20).reshape(N, 75)
    cc = c.ravel()
    e9, e21, e20 = ema_nb(cc, 9).reshape(N, 375), ema_nb(cc, 21).reshape(N, 375), ema_nb(cc, 20).reshape(N, 375)
    hh, ll = h.ravel(), l.ravel()
    tr = np.maximum(hh - ll, np.abs(hh - np.roll(cc, 1)))
    atr1 = ema_nb(tr, 30).reshape(N, 375)
    return dates, B, fac, st15, dir15, e5, e9, e21, e20, atr1


def htf_dir(dir15, e5, c, m):
    """15-minute Supertrend direction of the last CLOSED 15-minute candle at minute m (after its close), and whether
    the last closed 5-minute candle is on the same side of its EMA20 (0 if they disagree)."""
    k15 = (m + 1) // 15 - 1
    k5 = (m + 1) // 5 - 1
    if k15 < 0 or k5 < 0:
        return 0
    d = int(dir15[k15])
    c5 = c[5 * k5 + 4]
    return d if (c5 - e5[k5]) * d > 0 else 0


# ------------------------------------------------------------------ signals: each returns (i, sd, stop) or None
def orb_levels(h, l):
    H, L = h[:15].max(), l[:15].min()
    w = H - L
    return H, L, w, H + 0.2 * w, L - 0.2 * w


def sig_orb5(a, F, X, mode):
    """The app's opening-range rule (yesterday's direction, skip after a wide day) with 1-minute execution modes."""
    if not F or F["yra"] > 1.2:
        return None
    o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    H, L, w, up, dn = orb_levels(h, l)
    if w <= 0:
        return None
    sd = F["ydir"]
    trig, far = (up, L) if sd == 1 else (dn, H)
    beyond = (c > trig) if sd == 1 else (c < trig)
    if mode == "early":                                    # first 1-minute close beyond the trigger
        js = np.flatnonzero(beyond[15:LAST + 1])
        return (15 + js[0], sd, far) if len(js) else None
    ends = np.arange(19, LAST + 1, 5)                      # 5-minute closes from 09:35 to 14:00
    hit = ends[beyond[ends]]
    if not len(hit):
        return None
    i0 = int(hit[0])
    if mode == "base":
        return i0, sd, far
    if mode == "tight":                                     # same entry, stop under the last 5 one-minute candles
        stp = l[i0 - 4:i0 + 1].min() if sd == 1 else h[i0 - 4:i0 + 1].max()
        return (i0, sd, stp) if (c[i0] - stp) * sd >= 0.0003 * c[i0] else None
    if mode == "follow":                                    # the next 1-minute candle closes beyond the signal close
        j = i0 + 1
        return (j, sd, far) if j <= LAST and (c[j] - c[i0]) * sd > 0 else None
    if mode in ("retest", "retest_tight", "hammer"):       # wait up to 20 minutes for a pull back to the trigger
        touch = None
        for j in range(i0 + 1, min(i0 + 21, LAST + 1)):
            if (l[j] <= trig) if sd == 1 else (h[j] >= trig):
                touch = j
                break
        if touch is None:
            return None
        for j in range(touch, min(touch + 10, LAST + 1)):
            if (c[j] - far) * sd <= 0:
                return None                                 # reached the stop side first
            if mode == "hammer":
                body = abs(c[j] - o[j]); rngj = h[j] - l[j]
                wick = (min(o[j], c[j]) - l[j]) if sd == 1 else (h[j] - max(o[j], c[j]))
                ok = rngj > 0 and wick >= 2 * body and ((c[j] - l[j]) / rngj >= 0.6 if sd == 1 else (h[j] - c[j]) / rngj >= 0.6) \
                    and ((l[j] <= trig) if sd == 1 else (h[j] >= trig))
            else:
                ok = j > touch and (c[j] > max(h[j - 1], trig) if sd == 1 else c[j] < min(l[j - 1], trig))
            if ok:
                if mode == "retest":
                    return j, sd, far
                stp = l[touch:j + 1].min() if sd == 1 else h[touch:j + 1].max()
                return (j, sd, stp) if (c[j] - stp) * sd >= 0.0003 * c[j] else None
        return None
    raise ValueError(mode)


def sig_st15(a, F, X, mode):
    """Supertrend 15m flip (the app's second rule): first flip at a 15-minute close 09:45 - 14:00, stop at the line."""
    st15, dir15 = X["st15"], X["dir15"]
    prev = X["dir15_prev"]
    o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    for k in range(1, 19):
        d0 = dir15[k - 1] if k >= 1 else prev
        if dir15[k] != d0:
            i = 15 * k + 14
            sd = int(dir15[k])
            stop = st15[k]
            if (c[i] - stop) * sd <= 0:
                return None
            if mode == "base":
                return i, sd, stop
            if mode == "tight":
                stp = l[i - 4:i + 1].min() if sd == 1 else h[i - 4:i + 1].max()
                return (i, sd, stp) if (c[i] - stp) * sd >= 0.0003 * c[i] else None
            if mode == "retest":                            # wait for a 1-minute pull back and turn (up to 30 min)
                for j in range(i + 2, min(i + 31, LAST + 1)):
                    if (c[j] - stop) * sd <= 0:
                        return None
                    pulled = (l[i + 1:j].min() < c[i]) if sd == 1 else (h[i + 1:j].max() > c[i])
                    if pulled and ((c[j] > h[j - 1]) if sd == 1 else (c[j] < l[j - 1])):
                        return j, sd, stop
                return None
            return None
    return None


def sig_mtf(a, F, X, mode):
    """Multi-timeframe 1-minute setups: direction from the 15-minute Supertrend + 5-minute EMA20, entry on 1 minute."""
    o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    dir15, e5, e9, e21, e20, atr1 = X["dir15"], X["e5"], X["e9"], X["e21"], X["e20"], X["atr1"]
    twap = np.cumsum((h + l + c) / 3) / np.arange(1, 376)
    for j in range(31, LAST + 1):                           # from 09:46
        sd = htf_dir(dir15, e5, c, j - 1)
        if sd == 0:
            continue
        if mode == "ema_engulf":                             # pull back to the 1-minute EMA20, then an engulfing candle
            touched = (l[j - 5:j].min() <= e20[j - 5:j].max()) if sd == 1 else (h[j - 5:j].max() >= e20[j - 5:j].min())
            eng = (c[j] > o[j] and c[j - 1] < o[j - 1] and c[j] >= o[j - 1] and o[j] <= c[j - 1]) if sd == 1 else \
                  (c[j] < o[j] and c[j - 1] > o[j - 1] and c[j] <= o[j - 1] and o[j] >= c[j - 1])
            if touched and eng:
                stp = l[j - 5:j + 1].min() if sd == 1 else h[j - 5:j + 1].max()
                if (c[j] - stp) * sd >= 0.0003 * c[j]:
                    return j, sd, stp
        elif mode == "ema_cross":                            # 1-minute EMA 9/21 cross in the higher-timeframe direction
            if (e9[j] - e21[j]) * sd > 0 and (e9[j - 1] - e21[j - 1]) * sd <= 0:
                stp = l[j - 9:j + 1].min() if sd == 1 else h[j - 9:j + 1].max()
                if (c[j] - stp) * sd >= 0.0003 * c[j]:
                    return j, sd, stp
        elif mode == "twap_pull":                            # pull back to the day's TWAP, then a 1-minute turn
            touched = (l[j - 5:j].min() <= twap[j - 5:j].max()) if sd == 1 else (h[j - 5:j].max() >= twap[j - 5:j].min())
            turn = (c[j] > h[j - 1]) if sd == 1 else (c[j] < l[j - 1])
            if touched and turn and (c[j] - twap[j]) * sd > 0:
                stp = l[j - 5:j + 1].min() if sd == 1 else h[j - 5:j + 1].max()
                if (c[j] - stp) * sd >= 0.0003 * c[j]:
                    return j, sd, stp
        elif mode == "inside_break":                         # 1-minute inside candle, then a break of the mother candle
            if h[j - 1] < h[j - 2] and l[j - 1] > l[j - 2] and ((c[j] > h[j - 2]) if sd == 1 else (c[j] < l[j - 2])):
                stp = l[j - 2] if sd == 1 else h[j - 2]
                if (c[j] - stp) * sd >= 0.0003 * c[j]:
                    return j, sd, stp
        elif mode == "burst":                                # a big 1-minute candle closing near its extreme
            r = h[j] - l[j]
            if r >= 2.5 * atr1[j - 1] and ((c[j] - l[j]) / r >= 0.8 if sd == 1 else (h[j] - c[j]) / r >= 0.8) \
                    and (c[j] - o[j]) * sd > 0:
                stp = l[j] if sd == 1 else h[j]
                if (c[j] - stp) * sd >= 0.0003 * c[j]:
                    return j, sd, stp
    return None


def sig_orb1m(a, F, X, mode):
    """Opening range on 1-minute closes: first 5 minutes (ORB5) or first 15 minutes, yesterday's direction."""
    if not F or F["yra"] > 1.2:
        return None
    o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    n = 5 if mode == "orb5" else 15
    H, L = h[:n].max(), l[:n].min()
    w = H - L
    if w <= 0:
        return None
    sd = F["ydir"]
    trig, far = (H + 0.2 * w, L) if sd == 1 else (L - 0.2 * w, H)
    js = np.flatnonzero(((c > trig) if sd == 1 else (c < trig))[n:LAST + 1])
    return (n + js[0], sd, far) if len(js) else None


STRATS = {
    "ORB app: 5m close entry (baseline)": (sig_orb5, "base"),
    "ORB: first 1m close beyond trigger": (sig_orb5, "early"),
    "ORB: 5m signal + next 1m follow-through": (sig_orb5, "follow"),
    "ORB: wait for 1m retest of trigger, far stop": (sig_orb5, "retest"),
    "ORB: 1m retest, tight stop under retest": (sig_orb5, "retest_tight"),
    "ORB: 1m hammer on the retest, tight stop": (sig_orb5, "hammer"),
    "ORB: 5m entry, tight 1m stop (5-min low)": (sig_orb5, "tight"),
    "Supertrend 15m (baseline)": (sig_st15, "base"),
    "Supertrend: tight 1m stop": (sig_st15, "tight"),
    "Supertrend: wait for 1m pullback + turn": (sig_st15, "retest"),
    "MTF: 1m EMA20 pullback + engulfing": (sig_mtf, "ema_engulf"),
    "MTF: 1m EMA 9/21 cross": (sig_mtf, "ema_cross"),
    "MTF: 1m TWAP pullback + turn": (sig_mtf, "twap_pull"),
    "MTF: 1m inside-candle break": (sig_mtf, "inside_break"),
    "MTF: 1m momentum burst candle": (sig_mtf, "burst"),
    "ORB5 on 1m closes (first 5 min)": (sig_orb1m, "orb5"),
}
EX = ("hold", "t2", "trail1", "trail5")


def run():
    vix = lab.load_vix()
    rows = []
    for key in ("NIFTY", "SENSEX"):
        dates, B, fac, st15, dir15, e5, e9, e21, e20, atr1 = prepare(key)
        for k, d in enumerate(dates):
            F = fac[k]
            if not F:
                continue
            a = B[k]
            X = {"st15": st15[k], "dir15": dir15[k], "dir15_prev": dir15[k - 1][-1], "e5": e5[k], "e9": e9[k],
                 "e21": e21[k], "e20": e20[k], "atr1": atr1[k]}
            o, h, l, c = (np.ascontiguousarray(a[:, x]) for x in range(4))
            vx = vix.get(d)
            exp = odds.weekly_expiry_for(key, d)
            for name, (fn, mode) in STRATS.items():
                s = fn(a, F, X, mode)
                if s is None:
                    continue
                i, sd, stop = int(s[0]), int(s[1]), float(s[2])
                if (c[i] - stop) * sd <= 0:
                    continue
                res = exits_nb(o, h, l, c, i, sd, stop)
                iv = float(vx[min(i // 5, 71)] if vx is not None and np.isfinite(vx[min(i // 5, 71)]) else 14.0) / 100
                risk = (c[i] - stop) * sd
                for x, exn in enumerate(EX):
                    px, j = res[x]
                    rows.append((key, d, name, exn, sd, float(c[i]), i + 1, iv, exp, (px - c[i]) * sd / risk, risk, i,
                                 float(px), int(j) + 1, 1.0))
        print(key, "done", len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS)
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m)
    T.to_pickle("/home/claude/ml/m1/m1_trades.pkl")
    print("saved", T.shape)


if __name__ == "__main__":
    run()
