"""Previous day's high / low breakout, alone and with the app's opening-range rule.

Signal (intraday): the first candle that CLOSES above yesterday's high -> buy CE, below yesterday's low -> buy PE,
up to 14:00, at most one trade per index per day. Variants: 5- or 15-minute candles; from the open (a gap that holds
counts) or only from 09:30; any open or only days that open inside yesterday's range; four stops; exits hold (stop or
15:00), trail (after +1R the stop trails 1R behind the best price) and 2R target.
Signal (daily): yesterday CLOSED above the day before's high (bias up) / below its low (bias down).

Yesterday's high/low are the full session (09:15-15:30; from 3 Aug 2026 the 15:15+ auction candles are left out).
Option P&L as lab.py: 1 lot ATM, Black-Scholes with India VIX, weekly expiry, 0.5% slippage each way, charges;
"cal" = the app's time-decay model, "mkt" = all decay in market hours (stress). NIFTY 2015-2026, SENSEX 2018-2026.
Train = first 60% of dates, test = last 40%.
"""
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402

AUCTION_FROM = date(2026, 8, 3)


def daily_full(key):
    rows = json.loads((lab.H / f"{key}_5m.json").read_text())
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["m"] = ts.dt.hour * 60 + ts.dt.minute
    df["d"] = ts.dt.date
    df = df.drop_duplicates(["d", "m"])
    df = df[(df.m >= 555) & (df.m <= 925)]
    df = df[~((df.d >= AUCTION_FROM) & (df.m >= 915))]
    g = df.groupby("d")
    out = pd.DataFrame({"o": g.o.first(), "h": g.h.max(), "l": g.l.min(), "c": g.c.last(), "n": g.size()})
    return out[out.n >= 36]                      # regular sessions only (a Muhurat hour is not "yesterday")


def lean_exits(a, i, sd, stop):
    o, h, l, c = a.T
    e = c[i]
    risk = (e - stop) * sd
    if risk <= 0:
        return None, risk
    out = {}
    for ex in ("hold", "trail1", "t2"):
        st, best, res = stop, e, None
        for j in range(i + 1, lab.I1455 + 1):
            if (l[j] <= st) if sd == 1 else (h[j] >= st):
                res = ((min(st, o[j]) if sd == 1 else max(st, o[j])), j)
                break
            if ex == "t2" and ((h[j] if sd == 1 else l[j]) - e) * sd >= 2 * risk:
                res = (e + sd * 2 * risk, j)
                break
            best = max(best, h[j]) if sd == 1 else min(best, l[j])
            if ex == "trail1" and (best - e) * sd >= risk:
                st = max(st, best - risk) if sd == 1 else min(st, best + risk)
        out[ex] = res or (c[lab.I1455], lab.I1455)
    return out, risk


def pdhl_signal(a, ph, pl, atr, tf, start, gap, stop):
    o, h, l, c = a.T
    if gap == "inside" and not (pl <= o[0] <= ph):
        return None
    lo = 0 if start == "open" else 3
    if tf == 15:
        lo = max(lo, 2)
    H15, L15 = h[:3].max(), l[:3].min()
    for i in range(lo, lab.LAST_SIG + 1):
        if tf == 15 and (i + 1) % 3:
            continue
        if c[i] > ph:
            sd = 1
        elif c[i] < pl:
            sd = -1
        else:
            continue
        k0 = i - (tf // 5) + 1                     # first 5-minute candle of the signal candle
        if stop == "candle":
            stp = l[k0:i + 1].min() if sd == 1 else h[k0:i + 1].max()
        elif stop == "back":
            stp = (ph - 0.2 * atr) if sd == 1 else (pl + 0.2 * atr)
        elif stop == "30m":
            stp = l[max(0, i - 5):i + 1].min() if sd == 1 else h[max(0, i - 5):i + 1].max()
        else:                                      # far side of the first 15 minutes (only once it exists)
            if i < 3:
                continue
            stp = L15 if sd == 1 else H15
        if (c[i] - stp) * sd <= 0:
            return None
        return i, sd, stp
    return None


def main():
    vix = lab.load_vix()
    rows = []
    ctx = []                                         # per day: yesterday's range and the daily-close bias
    for key in ("NIFTY", "SENSEX"):
        days, ind, fac = lab.prepare(key)
        dl = daily_full(key)
        dds = list(dl.index)
        pos = {d: k for k, d in enumerate(dds)}
        for d in sorted(days):
            if d < lab.START[key] or "atr" not in fac[d] or d not in pos or pos[d] < 2:
                continue
            y, yy = dl.loc[dds[pos[d] - 1]], dl.loc[dds[pos[d] - 2]]
            ph, pl, atr = float(y.h), float(y.l), fac[d]["atr"]
            bias = 1 if y.c > yy.h else (-1 if y.c < yy.l else 0)
            ctx.append((key, d, ph, pl, atr, bias, float(days[d][0, 0])))
            a = days[d]
            vx = vix.get(d)
            exp = odds.weekly_expiry_for(key, d)
            for tf in (5, 15):
                for start in ("open", "0930"):
                    for gap in ("any", "inside"):
                        for stop in ("candle", "back", "30m", "orbfar"):
                            s = pdhl_signal(a, ph, pl, atr, tf, start, gap, stop)
                            if not s:
                                continue
                            i, sd, stp = s
                            ex, risk = lean_exits(a, i, sd, stp)
                            if ex is None:
                                continue
                            iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                            S0 = float(a[i, 3])
                            name = f"{tf}m|{start}|{gap}|{stop}"
                            for exn, (px, j) in ex.items():
                                rows.append((key, d, name, exn, sd, S0, (i + 1) * 5, iv, exp, (px - S0) * sd / risk,
                                             risk, i, float(px), (j + 1) * 5, 1.0))
        print(key, "done", len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS)
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m)
    T.to_pickle(lab.OUT / "pdhl_trades.pkl")
    pd.DataFrame(ctx, columns=["key", "date", "ph", "pl", "atr", "bias", "open"]).to_pickle(lab.OUT / "pdhl_ctx.pkl")
    print("saved", T.shape)


if __name__ == "__main__":
    main()
