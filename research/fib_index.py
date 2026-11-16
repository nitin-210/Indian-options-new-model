"""Fibonacci retracement entries on the current index rule (NIFTY Nov 2010 - Nov 2026, SENSEX Mar 2018 - Nov 2026).

Current rule (unchanged): range 09:15-09:30; signal = first 5-min close beyond the range +/- 0.2 x width, in yesterday's
direction, closing by 14:00; skip the day after a wide-range day (> 1.2x 14-day average); stop at the far side of the
range; exit at the stop or 15:00. Option P&L for exactly 1 lot (NIFTY 65, SENSEX 20) with the app's own option model
and charges (app.odds._option_estimate pieces), IV from India VIX at the entry.

Fibonacci variants use the SAME signals. After the signal, the leg runs from the day's extreme before the signal
(low for a CE, high for a PE) to the best price since the signal. A buy limit rests at the X% retracement of that leg
(recomputed as the leg extends); it fills when a later 5-min candle reaches it, until 14:00. Not filled = no trade.
  fib 38.2 / 50 / 61.8      stop = far side of the range (as now)
  fib 50, stop 78.6         stop = the 78.6% retracement (tighter)
  fib 61.8, stop 100        stop = the leg's start (the swing low/high)
  target 1.618 ext          enter as now, take profit at the 161.8% extension of the opening range
"""
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/build/india-options")
from app import bs, odds  # noqa: E402
from app.config import Settings  # noqa: E402
from app.costs import round_trip  # noqa: E402
from app.history import load_cached  # noqa: E402
from app.timeutil import IST, hm, ist  # noqa: E402

DATA = Path("/home/claude/bigdata/history")
TODAY = date(2026, 11, 7)
CTX = {"NIFTY": odds.Ctx("NIFTY", 65, 50.0, "NSE"), "SENSEX": odds.Ctx("SENSEX", 20, 100.0, "BSE")}
S = Settings()
FIVE = timedelta(minutes=5)


def opt_pnl(key, d, side, entry, exit_px, t_in, t_out, iv):
    """1-lot option P&L after charges, ATM at the entry (the app's model, without max-loss sizing)."""
    ctx = CTX[key]
    r = S.risk_free_rate_pct / 100.0
    exp = odds.weekly_expiry_for(ctx.index, d)
    K = round(entry / ctx.step) * ctx.step
    T0, T1 = bs.years_to_expiry(t_in, exp), bs.years_to_expiry(t_out, exp)
    slip = S.slippage_pct_no_quote / 100.0
    e_fill = bs.price(entry, K, T0, r, iv, side) * (1 + slip)
    x_fill = max(0.05, bs.price(exit_px, K, T1, r, iv, side) * (1 - slip))
    c = round_trip(e_fill, x_fill, ctx.lot, ctx.exch, S)
    return ctx.lot * (x_fill - e_fill) - c.total


def run(key):
    bars = load_cached(DATA, key)
    vix = load_cached(DATA, "VIX")
    vix_by = defaultdict(list)
    for b in vix:
        vix_by[b.start.astimezone(IST).date()].append(b)
    sessions = {d: b for d, b in odds.full_sessions(bars).items() if d < TODAY}
    prev = odds.yesterday_sides(bars)
    reg = odds.regular_sessions(bars)
    rds = sorted(reg)
    rng = {x: max(b.h for b in reg[x]) - min(b.l for b in reg[x]) for x in rds}
    yra = {}
    for i in range(13, len(rds) - 1):
        avg = sum(rng[x] for x in rds[i - 13:i + 1]) / 14
        yra[rds[i + 1]] = rng[rds[i]] / avg if avg > 0 else None
    out = []
    for d, day in sessions.items():
        side = prev.get(d)
        if side is None:
            continue
        ra = yra.get(d)
        if ra is None or ra > 1.2:
            continue
        st = np.array([b.start.astimezone(IST).hour * 60 + b.start.astimezone(IST).minute for b in day])
        o = np.array([b.o for b in day]); h = np.array([b.h for b in day])
        l = np.array([b.l for b in day]); c = np.array([b.c for b in day])
        H, L = h[:3].max(), l[:3].min()
        w = H - L
        if w <= 0:
            continue
        up, dn = H + 0.2 * w, L - 0.2 * w
        sd = 1 if side == "CE" else -1
        si = None
        for i in range(3, len(day)):
            if st[i] + 5 > 14 * 60:
                break
            if (c[i] > up) if sd == 1 else (c[i] < dn):
                si = i
                break
        if si is None:
            continue
        last = int(np.searchsorted(st, 14 * 60 + 55))          # the 14:55 candle (exit at its close, 15:00)
        stop_far = L if sd == 1 else H

        def iv_at(t):
            pr = [v for v in vix_by.get(d, []) if v.start <= t]
            return pr[-1].c / 100.0 if pr else S.default_iv_pct / 100.0

        def walk(entry, stop, k0, target=None):
            """from candle k0 on: stop / target / 15:00 -> (exit price, exit candle)"""
            for j in range(k0, last + 1):
                if (l[j] <= stop) if sd == 1 else (h[j] >= stop):
                    return (min(stop, o[j]) if sd == 1 else max(stop, o[j])), j, "stop"
                if target is not None and ((h[j] >= target) if sd == 1 else (l[j] <= target)):
                    return target, j, "target"
            return c[last], last, "time"

        def record(name, entry, stop, k_in, t_in, res):
            px, j, how = res
            t_out = day[j].start + FIVE
            risk = abs(entry - stop)
            out.append({"key": key, "date": d, "variant": name, "side": side, "entry": entry, "stop": stop,
                        "R_idx": (px - entry) * sd / risk if risk > 0 else 0.0, "how": how,
                        "pnl": opt_pnl(key, d, side, entry, px, t_in, t_out, iv_at(t_in)), "filled": True,
                        "t_in": t_in.astimezone(IST).strftime("%H:%M")})

        t_sig = day[si].start + FIVE
        # ---- current rule
        record("current", c[si], stop_far, si, t_sig, walk(c[si], stop_far, si + 1))
        # ---- target at the 161.8% extension of the opening range
        tgt = (L + 1.618 * w) if sd == 1 else (H - 1.618 * w)
        if (tgt - c[si]) * sd > 0:
            record("target 1.618 ext", c[si], stop_far, si, t_sig, walk(c[si], stop_far, si + 1, tgt))
        else:
            record("target 1.618 ext", c[si], stop_far, si, t_sig, (c[si], si, "target"))
        # ---- Fibonacci pullback entries
        A = l[:si + 1].min() if sd == 1 else h[:si + 1].max()        # the leg's start: the day's extreme so far
        for name, X, stop_kind in (("fib 38.2", 0.382, "far"), ("fib 50", 0.5, "far"), ("fib 61.8", 0.618, "far"),
                                   ("fib 50, stop 78.6", 0.5, "786"), ("fib 61.8, stop 100", 0.618, "100")):
            B = h[si] if sd == 1 else l[si]                           # best price so far (leg's end)
            filled = None
            for j in range(si + 1, last + 1):
                if st[j] + 5 > 14 * 60:
                    break
                lvl = B - X * (B - A) if sd == 1 else B + X * (A - B)
                if (l[j] <= lvl) if sd == 1 else (h[j] >= lvl):
                    entry = min(lvl, o[j]) if sd == 1 else max(lvl, o[j])     # a gap through the level fills at the open
                    if stop_kind == "far":
                        stop = stop_far
                    elif stop_kind == "786":
                        stop = B - 0.786 * (B - A) if sd == 1 else B + 0.786 * (A - B)
                    else:
                        stop = A
                    if (entry - stop) * sd <= 0:
                        break
                    # the same candle reaching the stop counts as stopped (cautious)
                    if (l[j] <= stop) if sd == 1 else (h[j] >= stop):
                        res = ((min(stop, o[j]) if sd == 1 else max(stop, o[j])), j, "stop")
                    else:
                        res = walk(entry, stop, j + 1)
                    record(name, entry, stop, j, day[j].start + FIVE, res)
                    filled = True
                    break
                B = max(B, h[j]) if sd == 1 else min(B, l[j])
            if not filled:
                out.append({"key": key, "date": d, "variant": name, "side": side, "filled": False, "pnl": 0.0,
                            "R_idx": 0.0, "how": "not filled"})
    return out


if __name__ == "__main__":
    rows = run("NIFTY") + run("SENSEX")
    df = pd.DataFrame(rows)
    df.to_pickle("/home/claude/ml/v2/fib_index.pkl")
    print(df.groupby("variant").size())
