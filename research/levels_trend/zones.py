"""The setup in the user's chart: a demand/supply ZONE that launched a strong move and gets retested, and a falling
(rising) TRENDLINE through lower highs (higher lows). NIFTY 2015-2026 and SENSEX 2018-2026, 5-minute candles.

Zones (built only from candles already closed - no hindsight):
  a 15-minute swing low (2 candles each side) after which price rallied at least 0.6x the average daily range (ADR)
  within an hour -> a demand zone from that low to 0.1 ADR above it. Supply zones mirror it. A zone lives for 3
  sessions, and dies when a 5-minute candle closes 0.05 ADR beyond its far edge.
Zone trade: price comes back into the zone after having been at least 0.4 ADR away from it, and a 5-minute candle
  closes back out of the zone in the bounce direction -> buy CE (demand) / PE (supply). Stop 0.05 ADR beyond the zone.
  Variants: every strong zone; only zones that ALREADY HELD once (a touch followed by a 0.4 ADR bounce), like the
  zone in the chart on its second and third touches.
Trendline trade: the two latest confirmed 15-minute swing highs of the last 3 sessions, the second lower (by 0.1 ADR or
  more) and at least an hour after the first -> a falling line. When price, after being 0.3 ADR below the line,
  touches it and a 5-minute candle closes back below -> buy PE; stop 0.05 ADR above the touch. Rising lines mirror.
Signals 09:30 - 14:00, first of the day per strategy. Exits and option P&L as lab.py.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402


def run_index(key, vix, MOVE=0.6, WIDTH=0.1, LIFE=3, AWAY=0.4):
    days, ind, fac = lab.prepare(key)
    ds = sorted(days)
    A = np.stack([days[d] for d in ds])                   # (N, 72, 4)
    N = len(ds)
    o, h, l, c = (A[:, :, k].ravel() for k in range(4))
    day_of = np.repeat(np.arange(N), 72)
    atr_d = np.array([fac[d].get("atr", np.nan) for d in ds])
    # 15-minute candles (24 a day) and their swing points, confirmed 2 candles later
    h15 = A[:, :, 1].reshape(N, 24, 3).max(2).ravel(); l15 = A[:, :, 2].reshape(N, 24, 3).min(2).ravel()
    piv_hi, piv_lo = [], []
    for g in range(2, len(h15) - 2):
        if h15[g] == h15[g - 2:g + 3].max():
            piv_hi.append(g)
        if l15[g] == l15[g - 2:g + 3].min():
            piv_lo.append(g)
    # zones: born when the strong move away is complete (5-minute index of that moment)
    zones = []                                            # (born5, day, side, lo, hi)
    for side, piv in ((1, piv_lo), (-1, piv_hi)):
        for g in piv:
            d = g // 24
            atr = atr_d[d]
            if not np.isfinite(atr):
                continue
            px = l15[g] if side == 1 else h15[g]
            s5 = (g + 1) * 3                               # first 5-minute candle after the swing candle
            for t in range(s5, min(s5 + 12, len(c))):
                if (h[t] - px >= MOVE * atr) if side == 1 else (px - l[t] >= MOVE * atr):
                    born = max(t, (g + 2) * 3 + 2)        # not before the swing is confirmed
                    lo, hi = (px, px + WIDTH * atr) if side == 1 else (px - WIDTH * atr, px)
                    zones.append((born, d, side, lo, hi))
                    break
    zones.sort()
    rows = []
    for k, d in enumerate(ds):
        if d < lab.START[key] or "atr" not in fac[d]:
            continue
        atr = fac[d]["atr"]
        base = k * 72
        live = [z for z in zones if z[0] < base and k - z[1] <= LIFE]
        a = days[d]
        vx = vix.get(d)
        exp = odds.weekly_expiry_for(key, d)
        found = {}
        # replay each live zone's history up to today's open, then today's candles
        for (born, zd, side, lo, hi) in live:
            armed, held, dead, touched_at = False, 0, False, None
            for t in range(born + 1, base + lab.LAST_SIG + 1):
                if t >= base + 72:
                    break
                if (c[t] < lo - 0.05 * atr) if side == 1 else (c[t] > hi + 0.05 * atr):
                    dead = True
                    break
                away = (l[t] - hi >= AWAY * atr) if side == 1 else (lo - h[t] >= AWAY * atr)
                if away:
                    if touched_at is not None:
                        held += 1                         # bounced 0.4 ADR after a touch
                        touched_at = None
                    armed = True
                into = (l[t] <= hi) if side == 1 else (h[t] >= lo)
                if into and armed:
                    out_ = (c[t] > hi) if side == 1 else (c[t] < lo)
                    i = t - base
                    if out_ and 3 <= i <= lab.LAST_SIG and t >= base:
                        stop = (min(l[t], lo) - 0.05 * atr) if side == 1 else (max(h[t], hi) + 0.05 * atr)
                        for name, ok in (("zone retest: any strong zone", True), ("zone retest: zone already held once", held >= 1)):
                            if ok and (name not in found or found[name][0] > i):
                                found[name] = (i, side, stop)
                    touched_at = t
                    armed = False
            if dead:
                continue
        # trendlines from the last two confirmed swing highs (falling) / lows (rising) of the last 3 sessions
        for side, piv, arr in ((-1, piv_hi, h15), (1, piv_lo, l15)):
            conf = [g for g in piv if (g + 2) * 3 + 2 < base and (g // 24) >= k - 3]
            if len(conf) < 2:
                continue
            g1, g2 = conf[-2], conf[-1]
            if g2 - g1 < 4:
                continue
            p1, p2 = arr[g1], arr[g2]
            if (p1 - p2 < 0.1 * atr) if side == -1 else (p2 - p1 < 0.1 * atr):
                continue
            slope = (p2 - p1) / ((g2 - g1) * 3)               # per 5-minute candle
            mid1 = g1 * 3 + 1
            line = lambda t: p1 + slope * (t - mid1)
            armed = False
            for t in range((g2 + 2) * 3 + 3, base + lab.LAST_SIG + 1):
                L = line(t)
                if (L - h[t] >= 0.3 * atr) if side == -1 else (l[t] - L >= 0.3 * atr):
                    armed = True
                broke = (c[t] > L + 0.1 * atr) if side == -1 else (c[t] < L - 0.1 * atr)
                if broke:
                    break
                touch = (h[t] >= L - 0.05 * atr and c[t] < L) if side == -1 else (l[t] <= L + 0.05 * atr and c[t] > L)
                if touch and armed:
                    i = t - base
                    if 3 <= i <= lab.LAST_SIG:
                        stop = (max(h[t], L) + 0.05 * atr) if side == -1 else (min(l[t], L) - 0.05 * atr)
                        nm = "trendline: sell the falling line" if side == -1 else "trendline: buy the rising line"
                        if nm not in found or found[nm][0] > i:
                            found[nm] = (i, side, stop)
                        break
                    armed = False
        for name, (i, sd, stop) in found.items():
            if (a[i, 3] - stop) * sd <= 0:
                continue
            ex, risk = lab.exits(a, i, sd, stop)
            if ex is None:
                continue
            iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
            for exn, (px, jj, _) in ex.items():
                if exn == "half1":
                    continue
                rows.append((key, d, name, exn, sd, float(a[i, 3]), (i + 1) * 5, iv, exp, (px - a[i, 3]) * sd / risk,
                             risk, i, float(px), (jj + 1) * 5, 1.0))
    print(key, len(rows), flush=True)
    return rows


def main():
    vix = lab.load_vix()
    rows = run_index("NIFTY", vix) + run_index("SENSEX", vix)
    T = pd.DataFrame(rows, columns=lab.COLS)
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m).astype("float32")
    T.to_pickle("/home/claude/ml/lab/zone_trades.pkl")
    alld = sorted(set(T.date)); cut = alld[int(len(alld) * .6)]
    allm = pd.period_range("2015-01", "2026-11", freq="M")
    print(f"\ntrain before {cut}")
    for (s, e), x in T.groupby(["strat", "exit"]):
        w = x.pnl_cal > 0
        d = pd.to_datetime(x.date)
        mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(allm, fill_value=0)
        yr = x.groupby(d.dt.year).pnl_cal.sum()
        print(f"{s:<38} {e:<6} {len(x)/len(allm):>4.1f}/mo | win {10*w.mean():.1f}/10 | avg win ₹{x.pnl_cal[w].mean():,.0f} "
              f"loss ₹{-x.pnl_cal[~w].mean():,.0f} | ₹/trade {x.pnl_cal.mean():+,.0f} (train {x[x.date<cut].pnl_cal.mean():+,.0f} "
              f"test {x[x.date>=cut].pnl_cal.mean():+,.0f}) | R {x.R.mean():+.2f} | ₹{mon.mean():+,.0f}/mo | losing yrs {int((yr<0).sum())} "
              f"| stress ₹{x.pnl_mkt.sum()/len(allm):+,.0f}/mo")


if __name__ == "__main__":
    main()
