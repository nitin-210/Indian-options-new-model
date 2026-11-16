"""Strategy lab: every intraday idea on NIFTY (2015-2026) and SENSEX (2018-2026) 5-minute candles, one simulator.

Each strategy gives at most one trade per index per day: a side, an entry at a 5-minute candle's close, and a stop.
Exits (all tested for every strategy):
  hold   stop or 15:00                       t1/t15/t2   take profit at 1 / 1.5 / 2 R (else stop or 15:00)
  be1    stop moves to entry after +1R        trail1      after +1R the stop trails 1R behind the best price
  half1  half sold at +1R, rest stop at entry
Option P&L: 1 lot ATM (NIFTY 65, SENSEX 20) bought at the entry, Black-Scholes with India VIX, weekly expiry, 0.5%
slippage each way and charges, under two time-decay assumptions: "cal" (calendar time, the app's model) and "mkt"
(all of a day's decay in market hours). Train = first 60% of days, test = the rest (chosen on train only).
"""
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

sys.path.insert(0, "/home/claude/build/india-options")
from app import odds  # noqa: E402

H = Path("/home/claude/bigdata/history")
OUT = Path("/home/claude/ml/lab")
NB = 72                      # 09:15 .. 15:10 five-minute candles
I1455 = 68                   # candle 14:55-15:00 (exit at its close)
LAST_SIG = 56                # last signal candle starts 13:55 (closes 14:00)
META = {"NIFTY": dict(lot=65, step=50.0, txn=0.03503), "SENSEX": dict(lot=20, step=100.0, txn=0.0325)}
START = {"NIFTY": date(2015, 1, 1), "SENSEX": date(2018, 3, 8)}
END = date(2026, 11, 7)


# ------------------------------------------------------------------ data
def load_days(key):
    rows = json.loads((H / f"{key}_5m.json").read_text())
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["m"] = ts.dt.hour * 60 + ts.dt.minute
    df["d"] = ts.dt.date
    df = df.drop_duplicates(["d", "m"])
    close_last = df[df.m <= 925].groupby("d")["c"].last()          # official close (last candle to 15:25)
    df = df[(df.m >= 555) & (df.m <= 910) & (df.m % 5 == 0)]
    days = {}
    for d, g in df.groupby("d", sort=True):
        if len(g) == NB:
            days[d] = g[["o", "h", "l", "c"]].to_numpy(float)
    return days, close_last


def load_vix():
    rows = json.loads((H / "VIX_5m.json").read_text())
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["d"], df["m"] = ts.dt.date, ts.dt.hour * 60 + ts.dt.minute
    out = {}
    for d, g in df.groupby("d"):
        a = np.full(NB, np.nan)
        mm = ((g.m.to_numpy() - 555) // 5).astype(int)
        ok = (mm >= 0) & (mm < NB)
        a[mm[ok]] = g.c.to_numpy()[ok]
        out[d] = pd.Series(a).ffill().bfill().to_numpy()
    return out


def ema(x, n):
    a = 2 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = ema(up, 2 * n - 1), ema(dn, 2 * n - 1)       # Wilder smoothing
    return 100 - 100 / (1 + au / np.maximum(ad, 1e-9))


def supertrend(h, l, c, n=10, mult=3.0):
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr = ema(tr, 2 * n - 1)
    mid = (h + l) / 2
    ub, lb = mid + mult * atr, mid - mult * atr
    st = np.zeros(len(c)); dirn = np.ones(len(c))
    fub, flb = ub.copy(), lb.copy()
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if dirn[i - 1] == 1:
            dirn[i] = -1 if c[i] < flb[i] else 1
        else:
            dirn[i] = 1 if c[i] > fub[i] else -1
        st[i] = flb[i] if dirn[i] == 1 else fub[i]
    return st, dirn


def prepare(key):
    days, close_last = load_days(key)
    ds = sorted(days)
    allbars = np.concatenate([days[d] for d in ds])
    o, h, l, c = allbars.T
    e9, e21, e20 = ema(c, 9), ema(c, 21), ema(c, 20)
    r14 = rsi(c, 14)
    sma20 = pd.Series(c).rolling(20).mean().to_numpy()
    sd20 = pd.Series(c).rolling(20).std().to_numpy()
    bbw = (4 * sd20) / sma20
    bbw_min = pd.Series(bbw).rolling(60).min().to_numpy()
    # 15-minute candles for supertrend and ema cross 15
    h15 = allbars[:, 1].reshape(-1, 3).max(1); l15 = allbars[:, 2].reshape(-1, 3).min(1); c15 = allbars[:, 3].reshape(-1, 3)[:, 2]
    st15, dir15 = supertrend(h15, l15, c15)
    e9_15, e21_15 = ema(c15, 9), ema(c15, 21)
    tr = np.maximum(h - l, np.abs(h - np.roll(c, 1)))
    atr5 = ema(tr, 27)
    bbu_all, bbl_all = sma20 + 2 * sd20, sma20 - 2 * sd20
    ind = {}
    for j, d in enumerate(ds):
        s = slice(j * NB, (j + 1) * NB)
        rep = lambda x: np.repeat(x[j * 24:(j + 1) * 24], 3)       # 15m value known at the 15m close: shift below
        ind[d] = dict(e9=e9[s], e21=e21[s], e20=e20[s], rsi=r14[s], bbu=bbu_all[s], bbl=bbl_all[s],
                      bbm=sma20[s], bbw=bbw[s], bbwmin=bbw_min[s], atr5=atr5[s],
                      st15=rep(st15), dir15=rep(dir15), e9_15=rep(e9_15), e21_15=rep(e21_15))
    # daily facts
    fac = {}
    rngs, closes, hist = [], [], []
    for j, d in enumerate(ds):
        a = days[d]
        f = {"open": a[0, 0], "high": a[:, 1].max(), "low": a[:, 2].min(), "close": float(close_last.get(d, a[-1, 3]))}
        if j >= 21:
            pd_ = fac[ds[j - 1]]
            f["pc"], f["ph"], f["pl"], f["po"] = pd_["close"], pd_["high"], pd_["low"], pd_["open"]
            rr = [fac[x]["high"] - fac[x]["low"] for x in ds[j - 14:j]]
            f["atr"] = float(np.mean(rr))
            f["yra"] = (pd_["high"] - pd_["low"]) / f["atr"]
            f["ydir"] = 1 if pd_["close"] > pd_["open"] else -1
            f["dma20"] = 1 if pd_["close"] > np.mean([fac[x]["close"] for x in ds[j - 20:j]]) else -1
            f["nr4"] = (pd_["high"] - pd_["low"]) <= min(fac[x]["high"] - fac[x]["low"] for x in ds[j - 4:j])
            f["inside"] = pd_["high"] <= fac[ds[j - 2]]["high"] and pd_["low"] >= fac[ds[j - 2]]["low"]
        fac[d] = f
    return days, ind, fac


# ------------------------------------------------------------------ strategies
def first(cond, lo, hi):
    for i in range(lo, hi + 1):
        if cond(i):
            return i
    return None


def orb(n_bars, buf, filt=None, stop="far"):
    def f(a, I, F):
        o, h, l, c = a.T
        H_, L_ = h[:n_bars].max(), l[:n_bars].min()
        w = H_ - L_
        if w <= 0:
            return None
        up, dn = H_ + buf * w, L_ - buf * w
        i = first(lambda i: c[i] > up or c[i] < dn, n_bars, LAST_SIG)
        if i is None:
            return None
        sd = 1 if c[i] > up else -1
        if filt and not filt(F, sd):
            return None
        stp = (L_ if sd == 1 else H_) if stop == "far" else (H_ + L_) / 2
        return i, sd, stp
    return f


def pdhl(a, I, F):
    o, h, l, c = a.T
    if "ph" not in F:
        return None
    i = first(lambda i: c[i] > F["ph"] or c[i] < F["pl"], 3, LAST_SIG)
    if i is None:
        return None
    sd = 1 if c[i] > F["ph"] else -1
    lo, hi = max(0, i - 6), i
    return i, sd, (l[lo:hi + 1].min() if sd == 1 else h[lo:hi + 1].max())


def nr_break(a, I, F):
    if not F.get("nr4") and not F.get("inside"):
        return None
    return pdhl(a, I, F)


def afternoon(a, I, F):
    o, h, l, c = a.T
    k = 45                                              # 09:15 .. 13:00 range = candles 0..44
    H_, L_ = h[:k].max(), l[:k].min()
    i = first(lambda i: c[i] > H_ or c[i] < L_, k, 63)  # until 14:30
    if i is None:
        return None
    sd = 1 if c[i] > H_ else -1
    return i, sd, (H_ + L_) / 2


def ema_cross(tf):
    def f(a, I, F):
        e9, e21 = (I["e9"], I["e21"]) if tf == 5 else (I["e9_15"], I["e21_15"])
        o, h, l, c = a.T
        lo = 6 if tf == 5 else 5
        step = 1 if tf == 5 else 3
        for i in range(lo, LAST_SIG + 1, step):
            j = i if tf == 5 else i                       # 15m values repeat over its 3 candles; use at the 15m close
            if tf == 15 and (i % 3) != 2:
                continue
            prev = i - step
            if (e9[prev] <= e21[prev] and e9[j] > e21[j]) or (e9[prev] >= e21[prev] and e9[j] < e21[j]):
                sd = 1 if e9[j] > e21[j] else -1
                lo_ = max(0, i - 6)
                return i, sd, (l[lo_:i + 1].min() if sd == 1 else h[lo_:i + 1].max())
        return None
    return f


def supertrend15(a, I, F):
    d = I["dir15"]
    o, h, l, c = a.T
    for i in range(5, LAST_SIG + 1, 3):
        if d[i] != d[i - 3]:
            return i, int(d[i]), I["st15"][i]
    return None


def first_hour(a, I, F):
    o, h, l, c = a.T
    i = 11                                              # candle 10:10-10:15
    mv = (c[i] - o[0]) / F["atr"] if F.get("atr") else 0
    if abs(mv) < 0.3:
        return None
    sd = 1 if mv > 0 else -1
    return i, sd, (l[:i + 1].min() if sd == 1 else h[:i + 1].max())


def twap(a):
    o, h, l, c = a.T
    tp = (h + l + c) / 3
    return np.cumsum(tp) / np.arange(1, len(tp) + 1)


def twap_trend(a, I, F):
    """after 10:15, in the day's direction (above/below open), the first candle that dips to TWAP and closes away"""
    o, h, l, c = a.T
    tw = twap(a)
    for i in range(12, LAST_SIG + 1):
        if c[i] > o[0] and c[i] > tw[i] and l[i] <= tw[i] and c[i - 1] > tw[i - 1]:
            return i, 1, min(l[i], tw[i] - 0.5 * I["atr5"][i])
        if c[i] < o[0] and c[i] < tw[i] and h[i] >= tw[i] and c[i - 1] < tw[i - 1]:
            return i, -1, max(h[i], tw[i] + 0.5 * I["atr5"][i])
    return None


def ema_pullback(a, I, F):
    """trend day (move from open > 0.3 ATR), after 10:30: a candle touching EMA20 and closing back in trend direction"""
    o, h, l, c = a.T
    e = I["e20"]
    for i in range(15, LAST_SIG + 1):
        mv = (c[i] - o[0]) / F["atr"] if F.get("atr") else 0
        if mv > 0.3 and l[i] <= e[i] and c[i] > e[i] and c[i] > o[i]:
            return i, 1, l[i] - 0.25 * I["atr5"][i]
        if mv < -0.3 and h[i] >= e[i] and c[i] < e[i] and c[i] < o[i]:
            return i, -1, h[i] + 0.25 * I["atr5"][i]
    return None


def rsi_rev(a, I, F):
    o, h, l, c = a.T
    r = I["rsi"]
    for i in range(9, LAST_SIG + 1):
        if r[i] < 20:
            return i, 1, l[:i + 1].min() - 0.25 * I["atr5"][i]
        if r[i] > 80:
            return i, -1, h[:i + 1].max() + 0.25 * I["atr5"][i]
    return None


def gap_fill(a, I, F):
    if "pc" not in F:
        return None
    o, h, l, c = a.T
    g = (o[0] - F["pc"]) / F["pc"]
    if abs(g) < 0.004:
        return None
    i = 2                                                # at 09:30
    sd = -1 if g > 0 else 1
    if (c[i] - F["pc"]) * (-sd) < abs(o[0] - F["pc"]) * 0.5:
        return None                                      # half the gap already filled: skip
    return i, sd, (h[:3].max() if sd == -1 else l[:3].min())


def orb_fail(a, I, F):
    o, h, l, c = a.T
    H_, L_ = h[:3].max(), l[:3].min()
    brk = first(lambda i: c[i] > H_ or c[i] < L_, 3, LAST_SIG)
    if brk is None:
        return None
    sd0 = 1 if c[brk] > H_ else -1
    for i in range(brk + 1, min(brk + 7, LAST_SIG + 1)):
        if (sd0 == 1 and c[i] < H_) or (sd0 == -1 and c[i] > L_):
            ext = h[brk:i + 1].max() if sd0 == 1 else l[brk:i + 1].min()
            return i, -sd0, ext
    return None


def bb_squeeze(a, I, F):
    o, h, l, c = a.T
    for i in range(6, LAST_SIG + 1):
        if I["bbw"][i - 1] <= I["bbwmin"][i - 1] * 1.1:
            if c[i] > I["bbu"][i]:
                return i, 1, I["bbm"][i]
            if c[i] < I["bbl"][i]:
                return i, -1, I["bbm"][i]
    return None


# --- candle patterns (5-minute), with the day's direction (vs open) and TWAP as context
def _pat(kind):
    def f(a, I, F):
        o, h, l, c = a.T
        tw = twap(a)
        rng = h - l
        avg = pd.Series(rng).rolling(12, min_periods=3).mean().to_numpy()
        body = np.abs(c - o)
        for i in range(6, LAST_SIG + 1):
            up_ctx = c[i] > o[0] and c[i] > tw[i]
            dn_ctx = c[i] < o[0] and c[i] < tw[i]
            if kind == "engulf":
                if up_ctx and c[i - 1] < o[i - 1] and c[i] > o[i] and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
                    return i, 1, min(l[i], l[i - 1])
                if dn_ctx and c[i - 1] > o[i - 1] and c[i] < o[i] and c[i] <= o[i - 1] and o[i] >= c[i - 1]:
                    return i, -1, max(h[i], h[i - 1])
            elif kind == "pinbar":
                lw, uw = min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])
                if up_ctx and lw >= 2 * max(body[i], 1e-9) and uw <= body[i] and rng[i] >= avg[i] and l[i] <= tw[i] + 0.5 * I["atr5"][i]:
                    return i, 1, l[i]
                if dn_ctx and uw >= 2 * max(body[i], 1e-9) and lw <= body[i] and rng[i] >= avg[i] and h[i] >= tw[i] - 0.5 * I["atr5"][i]:
                    return i, -1, h[i]
            elif kind == "star":
                small = body[i - 1] <= 0.3 * rng[i - 1]
                if small and c[i - 2] < o[i - 2] and body[i - 2] >= avg[i] * 0.6 and c[i] > o[i] and c[i] > (o[i - 2] + c[i - 2]) / 2 and up_ctx:
                    return i, 1, min(l[i - 2:i + 1])
                if small and c[i - 2] > o[i - 2] and body[i - 2] >= avg[i] * 0.6 and c[i] < o[i] and c[i] < (o[i - 2] + c[i - 2]) / 2 and dn_ctx:
                    return i, -1, max(h[i - 2:i + 1])
            elif kind == "three":
                if up_ctx and all(c[k] > o[k] and c[k] > c[k - 1] for k in (i - 2, i - 1, i)) and body[i] >= 0.5 * rng[i]:
                    return i, 1, l[i - 2]
                if dn_ctx and all(c[k] < o[k] and c[k] < c[k - 1] for k in (i - 2, i - 1, i)) and body[i] >= 0.5 * rng[i]:
                    return i, -1, h[i - 2]
            elif kind == "insidebar":
                if h[i - 1] <= h[i - 2] and l[i - 1] >= l[i - 2]:
                    if up_ctx and c[i] > h[i - 2]:
                        return i, 1, l[i - 1]
                    if dn_ctx and c[i] < l[i - 2]:
                        return i, -1, h[i - 1]
            elif kind == "marubozu":
                if rng[i] >= 1.5 * avg[i] and body[i] >= 0.8 * rng[i]:
                    if up_ctx and c[i] > o[i]:
                        return i, 1, l[i]
                    if dn_ctx and c[i] < o[i]:
                        return i, -1, h[i]
        return None
    return f


def with_filter(fn, filt):
    def f(a, I, F):
        s = fn(a, I, F)
        if s is None or not filt(F, s[1]):
            return None
        return s
    return f


YDAY = lambda F, sd: F.get("ydir") == sd
TREND = lambda F, sd: F.get("dma20") == sd
NOWIDE = lambda F, sd: F.get("yra") is not None and F["yra"] <= 1.2
OURS = lambda F, sd: YDAY(F, sd) and NOWIDE(F, sd)

STRATS = {
    "OUR MODEL (ORB15, yday, no wide day)": orb(3, 0.2, OURS),
    "ORB5": orb(1, 0.2), "ORB15": orb(3, 0.2), "ORB30": orb(6, 0.2), "ORB60": orb(12, 0.2),
    "ORB15 mid stop": orb(3, 0.2, stop="mid"),
    "ORB15 + 20-day trend": orb(3, 0.2, TREND), "ORB30 + yday": orb(6, 0.2, YDAY), "ORB60 + yday": orb(12, 0.2, YDAY),
    "yesterday high/low break": pdhl, "quiet-day (NR4/inside) break": nr_break, "afternoon break (13:00 range)": afternoon,
    "EMA 9/21 cross 5m": ema_cross(5), "EMA 9/21 cross 15m": ema_cross(15), "Supertrend 15m flip": supertrend15,
    "first-hour trend (10:15)": first_hour, "TWAP pullback in trend": twap_trend, "EMA20 pullback on trend day": ema_pullback,
    "RSI 20/80 reversal": rsi_rev, "gap fill": gap_fill, "failed ORB15 (fade)": orb_fail, "Bollinger squeeze break": bb_squeeze,
    "engulfing (with trend)": _pat("engulf"), "hammer/shooting star": _pat("pinbar"), "morning/evening star": _pat("star"),
    "three soldiers/crows": _pat("three"), "inside-bar break": _pat("insidebar"), "marubozu": _pat("marubozu"),
}
for k in list(STRATS):
    if k.startswith(("EMA", "Supertrend", "TWAP", "engulf", "hammer", "inside-bar", "marubozu", "first-hour", "afternoon")):
        STRATS[k + " + yday"] = with_filter(STRATS[k], YDAY)


# ------------------------------------------------------------------ exits
EXITS = ["hold", "t1", "t15", "t2", "be1", "trail1", "half1"]


def exits(a, i, sd, stop):
    """returns {exit: (price, candle index, half: (price, idx) or None)} in index terms"""
    o, h, l, c = a.T
    e = c[i]
    risk = abs(e - stop)
    out = {}
    if risk <= 0 or (stop - e) * sd >= 0:
        return None, risk
    for ex in EXITS:
        st, best, half = stop, e, None
        res = None
        tgt = {"t1": 1.0, "t15": 1.5, "t2": 2.0}.get(ex)
        for j in range(i + 1, I1455 + 1):
            if (l[j] <= st) if sd == 1 else (h[j] >= st):
                px = min(st, o[j]) if sd == 1 else max(st, o[j])
                res = (px, j, half)
                break
            fav_hi = h[j] if sd == 1 else l[j]
            if tgt and (fav_hi - e) * sd >= tgt * risk:
                res = (e + sd * tgt * risk, j, None)
                break
            best = max(best, h[j]) if sd == 1 else min(best, l[j])
            if ex in ("be1", "half1", "trail1") and (best - e) * sd >= risk:
                if ex == "half1" and half is None:
                    half = (e + sd * risk, j)
                if ex == "trail1":
                    st = max(st, best - risk) if sd == 1 else min(st, best + risk)
                else:
                    st = max(st, e) if sd == 1 else min(st, e)
        if res is None:
            res = (c[I1455], I1455, half)
        out[ex] = res
    return out, risk


# ------------------------------------------------------------------ option P&L (vectorized)
YEAR_S = 365.0 * 24 * 3600


def bs(S, K, T, sig, call, r=0.06):
    T = np.maximum(T, 1e-6)
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    disc = np.exp(-r * T)
    callp = S * ndtr(d1) - K * disc * ndtr(d2)
    putp = K * disc * ndtr(-d2) - S * ndtr(-d1)
    return np.where(call, callp, putp)


def t_years(dates, mins, expiries, mode):
    """mins = minutes after 09:15 of the moment; T to expiry 15:30"""
    d = np.array(dates, dtype="datetime64[D]")
    e = np.array(expiries, dtype="datetime64[D]")
    mins = np.asarray(mins, float)
    if mode == "cal":
        sec = (e - d).astype(int) * 86400 + (375 - mins) * 60
        return np.maximum(sec, 60) / YEAR_S
    full = np.busday_count(d + 1, e + 1)
    return np.maximum(full * 375 + (375 - mins), 1) / 375 / 252


def opt_pnl(tr, mode):
    """tr: DataFrame with key,date,sd,S0,m0,S1,m1,iv,(frac) -> rupees for 1 lot ATM after slippage + charges"""
    lot = tr.key.map(lambda k: META[k]["lot"]).to_numpy(float)
    step = tr.key.map(lambda k: META[k]["step"]).to_numpy(float)
    txn = tr.key.map(lambda k: META[k]["txn"]).to_numpy(float)
    K = np.round(tr.S0.to_numpy() / step) * step
    call = tr.sd.to_numpy() > 0
    exp = tr.exp.to_numpy()
    T0 = t_years(tr.date.to_numpy(), tr.m0.to_numpy(), exp, mode)
    T1 = t_years(tr.date.to_numpy(), tr.m1.to_numpy(), exp, mode)
    iv = tr.iv.to_numpy()
    p0 = bs(tr.S0.to_numpy(), K, T0, iv, call) * 1.005
    p1 = np.maximum(bs(tr.S1.to_numpy(), K, T1, iv, call), 0.05) * 0.995
    q = lot * tr.frac.to_numpy()
    turn = (p0 + p1) * q
    charges = 40 + 0.0015 * p1 * q + txn / 100 * turn + 1e-6 * turn + 0.00003 * p0 * q
    charges += 0.18 * (40 + txn / 100 * turn + 1e-6 * turn)
    return q * (p1 - p0) - charges


# ------------------------------------------------------------------ run
COLS = ["key", "date", "strat", "exit", "sd", "S0", "m0", "iv", "exp", "R", "risk", "t_in", "S1", "m1", "frac"]


def run():
    vix = load_vix()
    parts = []
    for key in ("NIFTY", "SENSEX"):
        days, ind, fac = prepare(key)
        rows = []
        for d in sorted(days):
            if d < START[key] or "atr" not in fac[d]:
                continue
            a, I, F = days[d], ind[d], fac[d]
            exp = odds.weekly_expiry_for(key, d)
            vx = vix.get(d)
            for name, fn in STRATS.items():
                try:
                    s = fn(a, I, F)
                except (IndexError, KeyError, ValueError, ZeroDivisionError):
                    s = None
                if s is None:
                    continue
                i, sd, stop = s
                ex, risk = exits(a, i, sd, stop)
                if ex is None:
                    continue
                iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                S0 = float(a[i, 3])
                for exn, (px, j, half) in ex.items():
                    R = (px - S0) * sd / risk
                    if exn == "half1" and half is not None:
                        rows.append((key, d, name, exn, sd, S0, (i + 1) * 5, iv, exp, 1.0, risk, i, float(half[0]), (half[1] + 1) * 5, 0.5))
                        rows.append((key, d, name, exn, sd, S0, (i + 1) * 5, iv, exp, 0.5 + 0.5 * R, risk, i, float(px), (j + 1) * 5, 0.5))
                    else:
                        rows.append((key, d, name, exn, sd, S0, (i + 1) * 5, iv, exp, R, risk, i, float(px), (j + 1) * 5, 1.0))
        df = pd.DataFrame(rows, columns=COLS)
        del rows
        for mode in ("cal", "mkt"):
            df[f"pnl_{mode}"] = opt_pnl(df, mode).astype("float32")
        df["strat"] = df["strat"].astype("category"); df["exit"] = df["exit"].astype("category")
        g = df.groupby(["key", "date", "strat", "exit"], sort=False, observed=True)
        agg = g.agg(sd=("sd", "first"), R=("R", "last"), risk=("risk", "first"), t_in=("t_in", "first"),
                    pnl_cal=("pnl_cal", "sum"), pnl_mkt=("pnl_mkt", "sum")).reset_index()
        parts.append(agg)
        print(key, "done", agg.shape, flush=True)
        del df, days, ind, fac
    out = pd.concat(parts, ignore_index=True)
    out.to_pickle(OUT / "lab_trades.pkl")
    print("saved", out.shape)


if __name__ == "__main__":
    run()
