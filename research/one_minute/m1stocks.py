"""The 1-minute tests on F&O stocks (72 stocks with 1-minute candles, 2017 - 2021).

Same rules as m1lab.py, but stock breakouts trade either side (the first break, as the dashboard's lists) and the
multi-timeframe pullback uses the real VWAP (stocks have volume). Results in R (risk = entry to stop, before costs)
and in rupees for an at-the-money monthly option on about Rs 5 lakh of stock (qty = 5,00,000 / price): Black-Scholes
with IV = 1.1 x the stock's 20-day realised volatility, 1% slippage each way (stock options are wider than NIFTY's)
and Indian charges.
"""
import glob
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, "/home/claude/ml/m1")
sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
import m1lab as M  # noqa: E402


def roll(x, n, fn):
    """fn over the n values ENDING at each index (NaN for the first n-1)"""
    out = np.full(len(x), np.nan)
    out[n - 1:] = fn(swv(x, n), axis=1)
    return out


def mtf_vec(a, X, mode):
    """Vectorised version of m1lab.sig_mtf (first qualifying minute from 09:46 to 14:00)."""
    o, h, l, c, v = (a[:, k] for k in range(5))
    dir15, e5, e9, e21, e20, atr1 = X["dir15"], X["e5"], X["e9"], X["e21"], X["e20"], X["atr1"]
    j = np.arange(375)
    k15, k5 = j // 15 - 1, j // 5 - 1
    ok = (k15 >= 0) & (k5 >= 0)
    d = np.where(ok, dir15[np.clip(k15, 0, 24)], 0)
    c5 = c[np.clip(5 * k5 + 4, 0, 374)]
    sd = np.where(ok & ((c5 - e5[np.clip(k5, 0, 74)]) * d > 0), d, 0).astype(int)
    lo5p, hi5p = np.roll(roll(l, 5, np.min), 1), np.roll(roll(h, 5, np.max), 1)        # previous 5 minutes
    lo6, hi6 = roll(l, 6, np.min), roll(h, 6, np.max)
    up, dn = sd == 1, sd == -1
    if mode == "ema_engulf":
        e20mx, e20mn = np.roll(roll(e20, 5, np.max), 1), np.roll(roll(e20, 5, np.min), 1)
        po, pc = np.roll(o, 1), np.roll(c, 1)
        eng_u = (c > o) & (pc < po) & (c >= po) & (o <= pc)
        eng_d = (c < o) & (pc > po) & (c <= po) & (o >= pc)
        hit = (up & (lo5p <= e20mx) & eng_u) | (dn & (hi5p >= e20mn) & eng_d)
        stop = np.where(up, lo6, hi6)
    elif mode == "vwap_pull":
        vw = np.cumsum((h + l + c) / 3 * np.maximum(v, 1)) / np.cumsum(np.maximum(v, 1))
        vmx, vmn = np.roll(roll(vw, 5, np.max), 1), np.roll(roll(vw, 5, np.min), 1)
        ph, pl = np.roll(h, 1), np.roll(l, 1)
        hit = (up & (lo5p <= vmx) & (c > ph) & (c > vw)) | (dn & (hi5p >= vmn) & (c < pl) & (c < vw))
        stop = np.where(up, lo6, hi6)
    elif mode == "ema_cross":
        s1, s0 = np.sign(e9 - e21), np.sign(np.roll(e9 - e21, 1))
        hit = (up & (s1 > 0) & (s0 <= 0)) | (dn & (s1 < 0) & (s0 >= 0))
        stop = np.where(up, roll(l, 10, np.min), roll(h, 10, np.max))
    elif mode == "inside_break":
        h1, l1, h2, l2 = np.roll(h, 1), np.roll(l, 1), np.roll(h, 2), np.roll(l, 2)
        ins = (h1 < h2) & (l1 > l2)
        hit = ins & ((up & (c > h2)) | (dn & (c < l2)))
        stop = np.where(up, l2, h2)
    elif mode == "burst":
        r = h - l
        a1 = np.roll(atr1, 1)
        top = np.where(r > 0, (c - l) / np.where(r > 0, r, 1), 0)
        bot = np.where(r > 0, (h - c) / np.where(r > 0, r, 1), 0)
        hit = (r >= 2.5 * a1) & ((up & (top >= 0.8) & (c > o)) | (dn & (bot >= 0.8) & (c < o)))
        stop = np.where(up, l, h)
    else:
        raise ValueError(mode)
    good = hit & ((c - stop) * sd >= 0.0003 * c) & (j >= 31) & (j <= M.LAST)
    good &= np.isfinite(stop)
    js = np.flatnonzero(good)
    if not len(js):
        return None
    i = int(js[0])
    return i, int(sd[i]), float(stop[i])


def orb_stock(a, X, mode):
    o, h, l, c = (a[:, k] for k in range(4))
    H, L, w, up, dn = M.orb_levels(h, l)
    if w <= 0:
        return None
    if mode == "early":
        js = np.flatnonzero(((c > up) | (c < dn))[15:M.LAST + 1])
        if not len(js):
            return None
        sd = 1 if c[15 + js[0]] > up else -1
    else:
        ends = np.arange(19, M.LAST + 1, 5)
        b = ends[(c[ends] > up) | (c[ends] < dn)]
        if not len(b):
            return None
        sd = 1 if c[b[0]] > up else -1
    return M.sig_orb5(a, {"ydir": sd, "yra": 0.0}, X, mode)


STRATS = {
    "ORB15 stock: 5m close entry (baseline)": ("orb", "base"),
    "ORB15 stock: first 1m close": ("orb", "early"),
    "ORB15 stock: next 1m follow-through": ("orb", "follow"),
    "ORB15 stock: 1m retest, far stop": ("orb", "retest"),
    "ORB15 stock: 1m retest, tight stop": ("orb", "retest_tight"),
    "ORB15 stock: 5m entry, tight 1m stop": ("orb", "tight"),
    "MTF stock: 1m EMA20 pullback + engulfing": ("mtf", "ema_engulf"),
    "MTF stock: 1m VWAP pullback + turn": ("mtf", "vwap_pull"),
    "MTF stock: 1m EMA 9/21 cross": ("mtf", "ema_cross"),
    "MTF stock: 1m inside-candle break": ("mtf", "inside_break"),
    "MTF stock: 1m momentum burst": ("mtf", "burst"),
}


def monthly_expiry(d):
    def last_thu(y, m):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        x = nxt - timedelta(days=1)
        return x - timedelta(days=(x.weekday() - 3) % 7)
    e = last_thu(d.year, d.month)
    if e < d:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        e = last_thu(y, m)
    return e


def stock_opt_pnl(T, mode="cal"):
    K = T.S0.to_numpy()                                    # at the money
    call = T.sd.to_numpy() > 0
    T0 = lab.t_years(T.date.to_numpy(), T.m0.to_numpy(), T.exp.to_numpy(), mode)
    T1 = lab.t_years(T.date.to_numpy(), T.m1.to_numpy(), T.exp.to_numpy(), mode)
    iv = T.iv.to_numpy()
    p0 = lab.bs(T.S0.to_numpy(), K, T0, iv, call) * 1.01
    p1 = np.maximum(lab.bs(T.S1.to_numpy(), K, T1, iv, call), 0.05) * 0.99
    q = 500000.0 / T.S0.to_numpy()
    turn = (p0 + p1) * q
    ch = 40 + 0.0015 * p1 * q + 0.03503 / 100 * turn + 1e-6 * turn + 0.00003 * p0 * q
    ch += 0.18 * (40 + 0.03503 / 100 * turn + 1e-6 * turn)
    return q * (p1 - p0) - ch


def run():
    parts = []
    files = sorted(glob.glob(f"{M.D}/stk_*.npz"))
    for n_, f in enumerate(files):
        key = f.split("/")[-1][:-4]
        dates, B, fac, st15, dir15, e5, e9, e21, e20, atr1 = M.prepare(key)
        closes = B[:, -1, 3]
        lr = np.diff(np.log(closes), prepend=np.nan)
        rows = []
        for k, d in enumerate(dates):
            if not fac[k] or k < 21:
                continue
            a = B[k]
            X = {"st15": st15[k], "dir15": dir15[k], "dir15_prev": dir15[k - 1][-1], "e5": e5[k], "e9": e9[k],
                 "e21": e21[k], "e20": e20[k], "atr1": atr1[k]}
            o, h, l, c = (np.ascontiguousarray(a[:, x]) for x in range(4))
            iv = max(0.15, 1.1 * np.nanstd(lr[k - 20:k]) * np.sqrt(252))
            exp = monthly_expiry(d)
            for name, (kind, mode) in STRATS.items():
                s = orb_stock(a, X, mode) if kind == "orb" else mtf_vec(a, X, mode)
                if s is None:
                    continue
                i, sd, stop = int(s[0]), int(s[1]), float(s[2])
                if (c[i] - stop) * sd <= 0:
                    continue
                res = M.exits_nb(o, h, l, c, i, sd, stop)
                risk = (c[i] - stop) * sd
                for x, exn in enumerate(M.EX):
                    px, j = res[x]
                    rows.append((key[4:], d, name, exn, sd, float(c[i]), i + 1, iv, exp, (px - c[i]) * sd / risk,
                                 risk, i, float(px), int(j) + 1, 1.0))
        T = pd.DataFrame(rows, columns=lab.COLS)
        T = T[T.exit != "trail5"]                         # same as trail1 within noise; saves disk
        T["pnl_cal"] = stock_opt_pnl(T, "cal").astype("float32")
        T["pnl_mkt"] = stock_opt_pnl(T, "mkt").astype("float32")
        T = T.drop(columns=["frac", "t_in", "S1", "m1", "m0", "exp", "iv", "S0"])
        T["R"] = T.R.astype("float32"); T["risk"] = T.risk.astype("float32")
        T["key"] = T.key.astype("category"); T["strat"] = T.strat.astype("category"); T["exit"] = T.exit.astype("category")
        parts.append(T)
        print(f"{n_ + 1}/{len(files)} {key} {len(T)}", flush=True)
    T = pd.concat(parts, ignore_index=True)
    for col in ("key", "strat", "exit"):
        T[col] = T[col].astype("category")
    T.to_pickle("/home/claude/ml/m1/m1_stock_trades.pkl.gz", compression="gzip")
    print("saved", T.shape)


if __name__ == "__main__":
    run()
