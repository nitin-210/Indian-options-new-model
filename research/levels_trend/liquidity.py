"""Liquidity pools, stop hunts and fake breakouts. NIFTY 2015-2026, SENSEX 2018-2026 (5-minute) with option P&L;
55 large-cap stocks Dec 2022 - Nov 2026 (R and option estimate) with --stocks.

Pools (where ordinary traders' stops sit), all known at the time:
  yesterday's high/low, last week's high/low, swing highs/lows of the last 3 sessions (15-minute pivots), EQUAL
  highs/lows (two or more swing points within 0.05 ADR of each other: double tops/bottoms), the opening range
  high/low (from 09:30), today's own swing highs/lows (5-minute pivots, once confirmed), and the falling/rising
  trendline through the last two lower highs / higher lows.
Trades (the first of the day per strategy, 09:30 - 14:00; the first-hour sweep from 09:20):
  sweep      a candle pokes 0.03 ADR or more beyond a pool and closes back on the other side -> trade the reversal
  fake       a candle CLOSES 0.05 ADR or more beyond a pool, and within 30 minutes a candle closes back inside ->
             trade the reversal; stop beyond the fake move's extreme
  sweep+MSS  after a sweep, price closes beyond the last minor swing on the other side within an hour ("market
             structure shift") -> trade the reversal; stop beyond the sweep
  judas      first hour (to 10:30): a sweep of yesterday's high (low) that closes back inside yesterday's range
  equal      sweep of an equal-highs/lows pool only
Each also "with trend": only when the reversal is in the direction of the 15-minute Supertrend (the user's "stop
hunt, then back to the trend") - and "against trend" for comparison.
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
from app import odds  # noqa: E402


def swings5(h, l, upto):
    """confirmed 5-minute swing highs/lows (2 candles each side) up to candle `upto` (exclusive of unconfirmed)"""
    hi = [h[k] for k in range(2, upto - 2) if h[k] == h[k - 2:k + 3].max()]
    lo = [l[k] for k in range(2, upto - 2) if l[k] == l[k - 2:k + 3].min()]
    return hi, lo


def equal_pools(vals, tol):
    v = sorted(vals)
    out = []
    for k in range(len(v) - 1):
        if v[k + 1] - v[k] <= tol:
            out.append((v[k], v[k + 1]))
    return out


def day_pools(prev_days, F, atr):
    """pre-open pools: list of (level, kind, side) - side +1 = stops ABOVE (resistance), -1 = stops BELOW (support)"""
    P = []
    ph, pl = F["ph"], F["pl"]
    P += [(ph, "yday", 1), (pl, "yday", -1)]
    wk = prev_days[-5:]
    P += [(max(a[:, 1].max() for a in wk), "week", 1), (min(a[:, 2].min() for a in wk), "week", -1)]
    sh, sl = SR.pivots15(prev_days[-3:])
    P += [(x, "swing", 1) for x in sh] + [(x, "swing", -1) for x in sl]
    for a_, b_ in equal_pools(sh, 0.05 * atr):
        P.append((max(a_, b_), "equal", 1))
    for a_, b_ in equal_pools(sl, 0.05 * atr):
        P.append((min(a_, b_), "equal", -1))
    return P


def trendlines(prev_days, atr):
    """the falling line through the last two lower 15-minute swing highs and the rising one through higher lows, as
    functions of today's 5-minute candle index (prev_days[-3:] then today)"""
    hs = np.concatenate([a[:, 1].reshape(-1, 3).max(1) for a in prev_days[-3:]])
    ls = np.concatenate([a[:, 2].reshape(-1, 3).min(1) for a in prev_days[-3:]])
    n15 = len(hs)
    out = []
    for arr, side in ((hs, 1), (ls, -1)):
        piv = [g for g in range(2, n15 - 2) if (arr[g] == arr[g - 2:g + 3].max() if side == 1 else arr[g] == arr[g - 2:g + 3].min())]
        if len(piv) < 2:
            continue
        g1, g2 = piv[-2], piv[-1]
        p1, p2 = arr[g1], arr[g2]
        if g2 - g1 < 4 or ((p1 - p2 < 0.1 * atr) if side == 1 else (p2 - p1 < 0.1 * atr)):
            continue
        slope = (p2 - p1) / ((g2 - g1) * 3)
        base = n15 * 3                                    # today's candle 0 in the same 5-minute count
        out.append((lambda i, p1=p1, g1=g1, slope=slope, base=base: p1 + slope * (base + i - g1 * 3 - 1), side))
    return out


def find(a, pools, lines, atr, dir15, kind, DEPTH=0.03, JEND=15, WIN=12):
    """first signal of the day: (i, sd, stop, with_trend) or None"""
    o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    start = 1 if kind == "judas" else 3
    end = JEND if kind == "judas" else lab.LAST_SIG
    for i in range(start, end + 1):
        lv = list(pools)
        if kind not in ("judas", "equal"):
            if i >= 3:
                lv += [(h[:3].max(), "orb", 1), (l[:3].min(), "orb", -1)]
            sh, sl = swings5(h, l, i)
            lv += [(x, "today", 1) for x in sh[-3:]] + [(x, "today", -1) for x in sl[-3:]]
            lv += [(f(i), "trend", s) for f, s in lines]
        if kind == "judas":
            lv = [p for p in lv if p[1] == "yday"]
        if kind == "equal":
            lv = [p for p in lv if p[1] == "equal"]
        k15 = (i + 1) // 3 - 1
        trend = int(dir15[k15]) if k15 >= 0 else 0
        for x, typ, side in lv:
            if side == 1 and not (c[i - 1] < x or kind == "fake"):
                continue
            if side == -1 and not (c[i - 1] > x or kind == "fake"):
                continue
            sd = -side                                    # the reversal: stops above -> buy PE, stops below -> CE
            if kind in ("sweep", "judas", "equal"):
                if (h[i] > x + DEPTH * atr and c[i] < x) if side == 1 else (l[i] < x - DEPTH * atr and c[i] > x):
                    stop = (h[i] + 0.02 * atr) if side == 1 else (l[i] - 0.02 * atr)
                    return i, sd, stop, sd == trend
            elif kind == "fake":
                # candle i closes back inside after a close beyond within the last 6 candles
                for b in range(max(1, i - 6), i):
                    beyond = (c[b] > x + 0.05 * atr and c[b - 1] <= x) if side == 1 else (c[b] < x - 0.05 * atr and c[b - 1] >= x)
                    if beyond and all(((c[m] > x) if side == 1 else (c[m] < x)) for m in range(b, i)) and \
                            ((c[i] < x) if side == 1 else (c[i] > x)):
                        stop = (h[b:i + 1].max() + 0.05 * atr) if side == 1 else (l[b:i + 1].min() - 0.05 * atr)
                        return i, sd, stop, sd == trend
            elif kind == "mss":
                pass
        if kind == "mss":
            # a sweep at candle s of a pool that EXISTED at s, then a close through the last minor swing before s
            for s_ in range(max(3, i - WIN), i):
                lv_s = list(pools) + [(h[:3].max(), "orb", 1), (l[:3].min(), "orb", -1)]
                sh_s, sl_s = swings5(h, l, s_)
                lv_s += [(x, "today", 1) for x in sh_s[-3:]] + [(x, "today", -1) for x in sl_s[-3:]]
                lv_s += [(f(s_), "trend", sd_) for f, sd_ in lines]
                for x, typ, side in lv_s:
                    swept = (h[s_] > x + DEPTH * atr and c[s_ - 1] < x) if side == 1 else (l[s_] < x - DEPTH * atr and c[s_ - 1] > x)
                    if not swept:
                        continue
                    ext = h[s_:i + 1].max() if side == 1 else l[s_:i + 1].min()
                    if (h[s_] < ext) if side == 1 else (l[s_] > ext):
                        continue                          # a later candle went further: not this sweep
                    ms = (sl_s[-1] if sl_s else None) if side == 1 else (sh_s[-1] if sh_s else None)
                    if ms is None:
                        continue
                    if (c[i] < ms and c[i - 1] >= ms) if side == 1 else (c[i] > ms and c[i - 1] <= ms):
                        sd = -side
                        stop = (ext + 0.02 * atr) if side == 1 else (ext - 0.02 * atr)
                        return i, sd, stop, sd == trend
    return None


KINDS = ("sweep", "fake", "mss", "judas", "equal")


def dir15_all(days, ds):
    A = np.stack([days[d][:, :4] for d in ds])
    N = len(ds)
    h15 = A[:, :, 1].reshape(N, -1, 3).max(2).ravel(); l15 = A[:, :, 2].reshape(N, -1, 3).min(2).ravel()
    c15 = A[:, :, 3].reshape(N, -1, 3)[:, :, -1].ravel()
    _, d15 = lab.supertrend(h15, l15, c15)
    return d15.reshape(N, -1)


def run_indices():
    vix = lab.load_vix()
    rows, stats = [], []
    for key in ("NIFTY", "SENSEX"):
        days, ind, fac = lab.prepare(key)
        ds = sorted(days)
        D15 = dir15_all(days, ds)
        for j, d in enumerate(ds):
            if d < lab.START[key] or "atr" not in fac[d] or j < 6:
                continue
            a, F = days[d], fac[d]
            atr = F["atr"]
            prev = [days[x] for x in ds[j - 5:j]]
            pools = day_pools(prev, F, atr)
            lines = trendlines(prev, atr)
            vx = vix.get(d)
            exp = odds.weekly_expiry_for(key, d)
            # how often does a close beyond yesterday's high/low fail (back inside within 30 minutes)?
            o, h, l, c = a.T
            for x, side in ((F["ph"], 1), (F["pl"], -1)):
                b = next((i for i in range(3, lab.LAST_SIG + 1) if ((c[i] > x) if side == 1 else (c[i] < x))), None)
                if b is not None and ((c[b - 1] <= x) if side == 1 else (c[b - 1] >= x)):
                    back = any(((c[m] < x) if side == 1 else (c[m] > x)) for m in range(b + 1, min(b + 7, 72)))
                    stats.append((key, d, "yday", back))
            for kind in KINDS:
                s = find(a, pools, lines, atr, D15[j], kind)
                if not s:
                    continue
                i, sd, stop, with_tr = s
                if (a[i, 3] - stop) * sd <= 0:
                    continue
                ex, risk = lab.exits(a, i, sd, stop)
                if ex is None:
                    continue
                iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                for exn, (px, jj, _) in ex.items():
                    if exn == "half1":
                        continue
                    for name in (kind, f"{kind} WITH trend" if with_tr else f"{kind} against trend"):
                        rows.append((key, d, name, exn, sd, float(a[i, 3]), (i + 1) * 5, iv, exp,
                                     (px - a[i, 3]) * sd / risk, risk, i, float(px), (jj + 1) * 5, 1.0))
        print(key, len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS)
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m).astype("float32")
    T.to_pickle("/home/claude/ml/lab/liq_trades.pkl")
    S = pd.DataFrame(stats, columns=["key", "date", "pool", "failed"])
    print(f"\nA close beyond yesterday's high/low came back inside within 30 minutes: {S.failed.mean():.0%} of {len(S)} breaks")
    report(T, months=pd.period_range("2015-01", "2026-11", freq="M"))


def report(T, months=None, rupee_label="option ₹/trade"):
    alld = sorted(set(T.date)); cut = alld[int(len(alld) * .6)]
    res = []
    for (s, e), x in T.groupby(["strat", "exit"]):
        w = x.pnl_cal > 0
        r = dict(strategy=s, exit=e, n=len(x), win10=10 * w.mean(), R=x.R.mean(), rs=x.pnl_cal.mean(),
                 train=x[x.date < cut].pnl_cal.mean(), test=x[x.date >= cut].pnl_cal.mean())
        if months is not None:
            d = pd.to_datetime(x.date)
            mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(months, fill_value=0)
            yr = x.groupby(d.dt.year).pnl_cal.sum()
            r.update(per_mo=len(x) / len(months), rs_mo=mon.mean(), lose_yrs=int((yr < 0).sum()),
                     stress=x.pnl_mkt.sum() / len(months), avg_win=x.pnl_cal[w].mean(), avg_loss=-x.pnl_cal[~w].mean())
        res.append(r)
    R = pd.DataFrame(res)
    pd.set_option("display.width", 250)
    best = R.loc[R.groupby("strategy").train.idxmax()].sort_values("test", ascending=False)
    print(f"\ntrain before {cut}; best exit per strategy chosen on train ({rupee_label}):")
    print(best.round(2).to_string(index=False))
    print("\npositive in BOTH train and test (any exit):", int(((R.train > 0) & (R.test > 0)).sum()), "of", len(R))
    return R


def run_stocks():
    from brk_events import load
    from m1stocks import monthly_expiry, stock_opt_pnl
    parts = []
    for f in sorted(glob.glob("/home/claude/ml/data5m/stocks5m/*_5m.json")):
        name = f.split("/")[-1].replace("_5m.json", "")
        days = load(f)
        ds = sorted(days)
        D15 = dir15_all(days, ds)
        closes = np.array([days[d][-1, 3] for d in ds])
        lr = np.diff(np.log(closes), prepend=np.nan)
        rows = []
        for j, d in enumerate(ds):
            if j < 21:
                continue
            a = days[d][:, :4]
            prev = [days[x][:, :4] for x in ds[j - 5:j]]
            atr = float(np.mean([days[x][:, 1].max() - days[x][:, 2].min() for x in ds[j - 14:j]]))
            if atr <= 0:
                continue
            F = {"ph": prev[-1][:, 1].max(), "pl": prev[-1][:, 2].min()}
            pools = day_pools(prev, F, atr)
            lines = trendlines(prev, atr)
            iv = max(0.15, 1.1 * np.nanstd(lr[j - 20:j]) * np.sqrt(252))
            exp = monthly_expiry(d)
            for kind in KINDS:
                s = find(a, pools, lines, atr, D15[j], kind)
                if not s:
                    continue
                i, sd, stop, with_tr = s
                if (a[i, 3] - stop) * sd <= 0:
                    continue
                ex, risk = lab.exits(a, i, sd, stop)
                if ex is None:
                    continue
                for exn in ("hold", "t1", "t2", "trail1"):
                    px, jj, _ = ex[exn]
                    for nm in (kind, f"{kind} WITH trend" if with_tr else f"{kind} against trend"):
                        rows.append((name, d, nm, exn, sd, float(a[i, 3]), (i + 1) * 5, iv, exp,
                                     (px - a[i, 3]) * sd / risk, risk, i, float(px), (jj + 1) * 5, 1.0))
        T = pd.DataFrame(rows, columns=lab.COLS)
        if len(T):
            T["pnl_cal"] = stock_opt_pnl(T, "cal").astype("float32")
            T["pnl_mkt"] = stock_opt_pnl(T, "mkt").astype("float32")
            parts.append(T[["key", "date", "strat", "exit", "R", "pnl_cal", "pnl_mkt"]])
        print(name, len(T), flush=True)
    T = pd.concat(parts, ignore_index=True)
    T.to_pickle("/home/claude/ml/lab/liq_stock_trades.pkl")
    report(T, rupee_label="stock option ₹/trade on ~₹5 lakh")


if __name__ == "__main__":
    run_stocks() if "--stocks" in sys.argv else run_indices()
