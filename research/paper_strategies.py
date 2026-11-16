"""Strategies from Chatterjee et al., "Options Trading Strategies for the Indian Market" (IJNRD, May 2022), tested on
NIFTY and SENSEX, Feb 2019 (weekly NIFTY options) - Nov 2026, 1 lot, 5-minute candles, option prices from the app's
Black-Scholes model with India VIX as volatility, 0.5% slippage per fill and the app's charges per leg.

The paper defines payoff structures and says which market view each suits; it gives no rule for WHEN to trade.
So: neutral structures are tested every day (no view needed); directional structures use simple, independent views
(not our model): 20-day trend, the opening gap, and the first hour's direction.

Structures (w = 2 strikes: NIFTY 100 pts, SENSEX 200 pts):
  neutral   long straddle (+CE +PE ATM) | short straddle | long call butterfly (+ITM -2 ATM +OTM) | short butterfly
  view up   long call | bull call spread (+ATM CE -OTM CE) | bull put spread (-ATM PE +OTM PE) | short put
  view down long put  | bear put spread  (+ATM PE -OTM PE) | bear call spread (-ATM CE +OTM CE) | short call
Holding: intraday (enter 09:30, or 10:15 for the first-hour view; exit 15:00) and the paper's way, held to expiry
(enter 09:30 on the first day of each weekly cycle, settle at the expiry-day close at intrinsic value).
Combined with our model: our signals with a spread instead of a single option, and the neutral structures on the
days our model does not trade.
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
from app.timeutil import IST  # noqa: E402

import os
TIME_MODE = os.environ.get("TIME_MODE", "calendar")
if TIME_MODE == "trading":
    # all of a day's time decay inside market hours (09:15-15:30): T = trading days left / 252 (weekends count 0)
    def _trading_T(now, expiry):
        from datetime import time as _t
        end = datetime.combine(expiry, _t(15, 30), tzinfo=IST)
        now = now.astimezone(IST)
        if now >= end:
            return 1e-6
        mins, d = 0.0, now.date()
        while d <= expiry:
            if d.weekday() < 5:
                a = max(now, datetime.combine(d, _t(9, 15), tzinfo=IST))
                e = datetime.combine(d, _t(15, 30), tzinfo=IST)
                if e > a:
                    mins += (e - a).total_seconds() / 60
            d += timedelta(days=1)
        return max(mins / 375 / 252, 1e-6)
    bs.years_to_expiry = _trading_T
DATA = Path("/home/claude/bigdata/history")
START, TODAY = date(2019, 2, 11), date(2026, 11, 7)
META = {"NIFTY": (65, 50.0, "NSE"), "SENSEX": (20, 100.0, "BSE")}
S = Settings()
R = S.risk_free_rate_pct / 100
SLIP = S.slippage_pct_no_quote / 100
FIVE = timedelta(minutes=5)

NEUTRAL = {"long straddle": [(+1, "CE", 0), (+1, "PE", 0)],
           "short straddle": [(-1, "CE", 0), (-1, "PE", 0)],
           "long butterfly": [(+1, "CE", -2), (-2, "CE", 0), (+1, "CE", 2)],
           "short butterfly": [(-1, "CE", -2), (+2, "CE", 0), (-1, "CE", 2)]}
UP = {"long option": [(+1, "CE", 0)], "debit spread": [(+1, "CE", 0), (-1, "CE", 2)],
      "credit spread": [(-1, "PE", 0), (+1, "PE", -2)], "short option": [(-1, "PE", 0)]}
DOWN = {"long option": [(+1, "PE", 0)], "debit spread": [(+1, "PE", 0), (-1, "PE", -2)],
        "credit spread": [(-1, "CE", 0), (+1, "CE", 2)], "short option": [(-1, "CE", 0)]}


def legs_pnl(key, legs, S0, S1, t0, t1, iv, expiry, at_expiry=False):
    """P&L in rupees for 1 lot of every leg, after slippage and charges."""
    lot, step, exch = META[key]
    atm = round(S0 / step) * step
    T0 = bs.years_to_expiry(t0, expiry)
    T1 = bs.years_to_expiry(t1, expiry)
    tot = 0.0
    for q, opt, k in legs:
        K = atm + k * step
        p0 = bs.price(S0, K, T0, R, iv, opt)
        p1 = max(S1 - K, 0.0) if (at_expiry and opt == "CE") else max(K - S1, 0.0) if at_expiry else bs.price(S1, K, T1, R, iv, opt)
        n = abs(q) * lot
        if q > 0:      # bought, then sold
            e, x = p0 * (1 + SLIP), max(0.05, p1 * (1 - SLIP)) if not at_expiry else p1
            tot += n * (x - e) - round_trip(e, x, n, exch, S).total
        else:          # sold, then bought back
            e, x = p0 * (1 - SLIP), (p1 * (1 + SLIP) if not at_expiry else p1)
            tot += n * (e - x) - round_trip(x, e, n, exch, S).total
    return tot


def load(key):
    bars = load_cached(DATA, key)
    vix = load_cached(DATA, "VIX")
    vix_by = defaultdict(list)
    for b in vix:
        vix_by[b.start.astimezone(IST).date()].append(b)
    sessions = {d: b for d, b in odds.full_sessions(bars).items() if START <= d < TODAY}
    return bars, sessions, vix_by


def iv_at(vix_by, d, t):
    pr = [v for v in vix_by.get(d, []) if v.start + FIVE <= t]
    return pr[-1].c / 100.0 if pr else S.default_iv_pct / 100.0


def run(key):
    bars, sessions, vix_by = load(key)
    ds = sorted(sessions)
    closes = {d: sessions[d][-1].c for d in ds}
    allc = odds.day_open_close(bars)                    # for the 20-day trend (regular sessions)
    cds = sorted(allc)
    prev = odds.yesterday_sides(bars)
    reg = odds.regular_sessions(bars)
    rds = sorted(reg)
    rng = {x: max(b.h for b in odds.continuous(x, reg[x])) - min(b.l for b in odds.continuous(x, reg[x])) for x in rds}
    yra = {}
    for i in range(13, len(rds) - 1):
        yra[rds[i + 1]] = rng[rds[i]] / (sum(rng[x] for x in rds[i - 13:i + 1]) / 14)
    rows = []
    for d in ds:
        day = sessions[d]
        st = [b.start.astimezone(IST) for b in day]
        hm_ = [t.hour * 60 + t.minute for t in st]
        idx = {m: i for i, m in enumerate(hm_)}
        if 555 + 10 not in idx or 895 not in idx or 615 not in idx:
            continue
        i930, i1015, i1455 = idx[565], idx[615], idx[895]      # candles ending 09:30, 10:20 (start 10:15), 15:00
        S930, S1015, S1500 = day[i930].c, day[idx[610]].c, day[i1455].c
        t930, t1015, t1500 = st[i930] + FIVE, st[idx[610]] + FIVE, st[i1455] + FIVE
        exp = odds.weekly_expiry_for(key, d)
        iv930, iv1015 = iv_at(vix_by, d, t930), iv_at(vix_by, d, t1015)
        # independent views
        j = cds.index(d) if d in allc else None
        trend = None
        if j is not None and j >= 21:
            sma = np.mean([allc[x][1] for x in cds[j - 20:j]])
            trend = 1 if allc[cds[j - 1]][1] > sma else -1
        pcs = [x for x in cds if x < d]
        gap = None
        if pcs:
            g = day[0].o - allc[pcs[-1]][1]
            gap = 1 if g > 0 else (-1 if g < 0 else None)
        fh = 1 if S1015 > day[0].o else (-1 if S1015 < day[0].o else None)
        base = {"key": key, "date": d}
        for name, legs in NEUTRAL.items():
            rows.append({**base, "mode": "intraday", "view": "none", "structure": name,
                         "pnl": legs_pnl(key, legs, S930, S1500, t930, t1500, iv930, exp)})
        for vname, v, S0, t0, iv in (("20-day trend", trend, S930, t930, iv930), ("opening gap", gap, S930, t930, iv930),
                                     ("first hour", fh, S1015, t1015, iv1015)):
            if v is None:
                continue
            for name, legs in (UP if v > 0 else DOWN).items():
                rows.append({**base, "mode": "intraday", "view": vname, "structure": name,
                             "pnl": legs_pnl(key, legs, S0, S1500, t0, t1500, iv, exp)})
        # our model's trade (same rule as the app; for the "combined" tests)
        side = prev.get(d)
        ra = yra.get(d)
        sig = None
        if side and ra is not None and ra <= 1.2:
            H, L = max(b.h for b in day[:3]), min(b.l for b in day[:3])
            w = H - L
            up_, dn_ = H + 0.2 * w, L - 0.2 * w
            sd = 1 if side == "CE" else -1
            for i in range(3, len(day)):
                if hm_[i] + 5 > 840:
                    break
                if (day[i].c > up_) if sd == 1 else (day[i].c < dn_):
                    sig = (i, sd, L if sd == 1 else H)
                    break
        if sig:
            i, sd, stop = sig
            px, t_out = S1500, t1500
            for k in range(i + 1, i1455 + 1):
                b = day[k]
                if (b.l <= stop) if sd == 1 else (b.h >= stop):
                    px, t_out = (min(stop, b.o) if sd == 1 else max(stop, b.o)), st[k] + FIVE
                    break
            S0, t0 = day[i].c, st[i] + FIVE
            iv = iv_at(vix_by, d, t0)
            for name, legs in (UP if sd > 0 else DOWN).items():
                rows.append({**base, "mode": "our signal", "view": "our model", "structure": name,
                             "pnl": legs_pnl(key, legs, S0, px, t0, t_out, iv, exp)})
        else:
            rows.append({**base, "mode": "our signal", "view": "our model", "structure": "no trade", "pnl": 0.0})
    # held to expiry, the paper's way: enter 09:30 on the first day of each weekly cycle
    last_exp = None
    for d in ds:
        exp = odds.weekly_expiry_for(key, d)
        if exp == last_exp:
            continue
        last_exp = exp
        settle_days = [x for x in ds if d < x <= exp]
        if not settle_days or (exp - settle_days[-1]).days > 3:
            continue
        sd_ = settle_days[-1]
        day = sessions[d]
        st = [b.start.astimezone(IST) for b in day]
        i930 = next((i for i, t in enumerate(st) if t.hour * 60 + t.minute == 565), None)
        if i930 is None:
            continue
        S0, t0 = day[i930].c, st[i930] + FIVE
        S1 = closes[sd_]
        t1 = datetime.combine(sd_, datetime.min.time(), tzinfo=IST).replace(hour=15, minute=30)
        iv = iv_at(vix_by, d, t0)
        allc_d = [x for x in cds if x < d]
        trend = None
        if len(allc_d) >= 21:
            trend = 1 if allc[allc_d[-1]][1] > np.mean([allc[x][1] for x in allc_d[-20:]]) else -1
        base = {"key": key, "date": d}
        for name, legs in NEUTRAL.items():
            rows.append({**base, "mode": "to expiry", "view": "none", "structure": name,
                         "pnl": legs_pnl(key, legs, S0, S1, t0, t1, iv, exp, at_expiry=True)})
        if trend:
            for name, legs in (UP if trend > 0 else DOWN).items():
                rows.append({**base, "mode": "to expiry", "view": "20-day trend", "structure": name,
                             "pnl": legs_pnl(key, legs, S0, S1, t0, t1, iv, exp, at_expiry=True)})
    return rows


if __name__ == "__main__":
    df = pd.DataFrame(run("NIFTY") + run("SENSEX"))
    df.to_pickle("/home/claude/ml/v2/paper_strategies.pkl" if TIME_MODE == "calendar" else "/home/claude/ml/v2/paper_strategies_trading.pkl")
    print(df.groupby(["mode", "view", "structure"]).size().to_string())
