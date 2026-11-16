"""Synthetic demo session (clearly labelled) and the option pricing used by
replays that only have index candles.

Nothing here is real market data. It exists so you can test the whole app
outside market hours: a NIFTY breakout held to the 15:00 exit, a SENSEX
breakdown that gets stopped out, and a short feed drop.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np

from . import bs
from .candles import Bar
from .instruments import Contract
from .timeutil import ist, hm

DEMO_DAY = date(2026, 11, 4)  # a Wednesday; NIFTY expiry Mon 9 Nov (holiday shift), SENSEX Thu 5 Nov
OPEN_S = 9 * 3600 + 15 * 60
CLOSE_S = 15 * 3600 + 30 * 60
N_SEC = CLOSE_S - OPEN_S  # 22,500 seconds

SCENARIOS = {
    "NIFTY": {"prev_close": 25560.0, "vol": 0.55, "anchors": [
        ("09:15", 25580), ("09:18", 25612), ("09:22", 25560), ("09:27", 25598), ("09:30", 25594),
        ("09:36", 25588), ("09:44", 25600), ("09:49", 25642), ("10:10", 25664), ("10:40", 25648),
        ("11:20", 25692), ("11:50", 25706), ("12:20", 25758), ("13:10", 25738), ("14:10", 25722),
        ("15:00", 25730), ("15:30", 25734)]},
    "SENSEX": {"prev_close": 83820.0, "vol": 1.9, "anchors": [
        ("09:15", 83800), ("09:19", 83885), ("09:24", 83715), ("09:29", 83790), ("09:30", 83784),
        ("09:45", 83770), ("10:05", 83728), ("10:18", 83640), ("10:45", 83610), ("11:15", 83660),
        ("11:45", 83742), ("12:15", 83815), ("13:00", 83880), ("14:00", 83905), ("15:00", 83930),
        ("15:30", 83940)]},
    # NIFTY BANK: a clean upside breakout that keeps going (a "big day" in the demo)
    "BANKNIFTY": {"prev_close": 57520.0, "vol": 1.4, "anchors": [
        ("09:15", 57560), ("09:19", 57642), ("09:23", 57530), ("09:28", 57610), ("09:30", 57602),
        ("09:40", 57630), ("09:52", 57735), ("10:15", 57790), ("10:45", 57760), ("11:20", 57880),
        ("12:00", 57960), ("12:40", 58040), ("13:30", 58010), ("14:15", 58090), ("15:00", 58120),
        ("15:30", 58110)]},
}
VIX_ANCHORS = [("09:15", 13.6), ("10:30", 13.2), ("12:00", 13.4), ("15:30", 13.1)]


def _sec(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60 - OPEN_S


def bridge_path(anchors, vol: float, seed: int) -> np.ndarray:
    """Per-second path through the anchors with Brownian-bridge noise."""
    rng = np.random.default_rng(seed)
    out = np.empty(N_SEC + 1)
    pts = [(_sec(t), float(v)) for t, v in anchors]
    for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
        n = s1 - s0
        w = np.concatenate([[0.0], np.cumsum(rng.normal(0, vol, n))])
        t = np.arange(n + 1) / n
        out[s0:s1 + 1] = v0 + (v1 - v0) * t + (w - t * w[-1])
    return np.round(out, 2)


def path_from_minutes(bars1m: list[Bar], close_sec: int = 60) -> np.ndarray:
    """Per-second index path through each real 1-minute candle: open -> high/low -> close.
    close_sec=59 puts the close inside the candle's own minute (used for stocks, sampled a few times a minute)."""
    out = np.full(N_SEC + 1, np.nan)
    for b in bars1m:
        s0 = int((b.start - ist(b.start.date(), hm("09:15"))).total_seconds())
        if s0 < 0 or s0 + 60 > N_SEC:
            continue
        first, second = (b.l, b.h) if b.c >= b.o else (b.h, b.l)
        knots = [(0, b.o), (20, first), (40, second), (close_sec, b.c)]
        for (a, va), (z, vz) in zip(knots, knots[1:]):
            out[s0 + a: s0 + z + 1] = np.linspace(va, vz, z - a + 1)
    idx = np.arange(len(out))
    good = ~np.isnan(out)
    if good.any():
        out = np.interp(idx, idx[good], out[good])
    return np.round(out, 2)


def _weekly(index: str, d: date, n: int, holidays=()) -> list[date]:
    """n expiries on or after d (weekly: Tuesday NIFTY, Thursday SENSEX; NIFTY BANK monthly, last Tuesday).
    An expiry that falls on a holiday moves to the trading day before it, as the exchanges do."""
    def shift(e: date) -> date:
        while e.weekday() >= 5 or e in holidays:
            e -= timedelta(days=1)
        return e

    out: list[date] = []
    if index == "BANKNIFTY":
        x = date(d.year, d.month, 1)
        while len(out) < n:
            nxt = date(x.year + (x.month == 12), x.month % 12 + 1, 1)
            last = nxt - timedelta(days=1)
            e = shift(last - timedelta(days=(last.weekday() - 1) % 7))
            if e >= d:
                out.append(e)
            x = nxt
        return out
    wd = 1 if index == "NIFTY" else 3
    e = d - timedelta(days=(d.weekday() - wd) % 7)       # the scheduled expiry on or before d
    while len(out) < n:
        x = shift(e)
        if x >= d:
            out.append(x)
        e += timedelta(days=7)
    return out


def demo_contracts(day: date | None = None, centers: dict[str, float] | None = None, holidays=()) -> list[Contract]:
    """Synthetic option contracts. For the built-in demo day the expiries match the real
    calendar (NIFTY moved to Mon 9 Nov because Tue 10 Nov is a holiday)."""
    out: list[Contract] = []
    n = 0
    if day is None or day == DEMO_DAY:
        exps = {"NIFTY": [date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 23), date(2026, 12, 1)],
                "SENSEX": [date(2026, 11, 5), date(2026, 11, 12), date(2026, 11, 19), date(2026, 11, 26)],
                "BANKNIFTY": [date(2026, 11, 24), date(2026, 12, 29), date(2027, 1, 26)]}
        centers = centers or {"NIFTY": 25600.0, "SENSEX": 83800.0, "BANKNIFTY": 57600.0}
    else:
        exps = {k: _weekly(k, day, 4, holidays) for k in ("NIFTY", "SENSEX", "BANKNIFTY")}
        centers = centers or {"NIFTY": 25600.0, "SENSEX": 83800.0, "BANKNIFTY": 57600.0}
    centers = {**{"NIFTY": 25600.0, "SENSEX": 83800.0, "BANKNIFTY": 57600.0}, **centers}
    meta = {"NIFTY": (50, 65, "NFO", 40), "SENSEX": (100, 20, "BFO", 40), "BANKNIFTY": (100, 30, "NFO", 40)}
    for name, (step, lot, exch, each) in meta.items():
        mid = round(centers[name] / step) * step
        for e in exps[name]:
            for i in range(-each, each + 1):
                k = mid + i * step
                for opt in ("CE", "PE"):
                    n += 1
                    sym = f"{name}{e.strftime('%d%b%y').upper()}{int(k)}{opt}"
                    out.append(Contract(f"D{n:05d}", sym, name, e, float(k), opt, lot, 0.05, exch))
    return out


def smile_iv(base: float, S: float, K: float) -> float:
    m = math.log(K / S)
    return base * (1.0 + 2.2 * abs(m)) + base * 1.4 * max(0.0, -m)


def option_quote(c: Contract, S: float, now: datetime, vix: float, r: float, rng_val: float) -> dict:
    iv = smile_iv(vix / 100.0 * 1.04, S, c.strike) * (1.0 + 0.01 * rng_val)
    T = bs.years_to_expiry(now, c.expiry)
    p = max(0.05, bs.price(S, c.strike, T, r, iv, c.opt))
    ltp = round(round(p / 0.05) * 0.05, 2)
    half = max(0.05, round(ltp * 0.002 / 0.05) * 0.05)
    bid, ask = max(0.05, round(ltp - half, 2)), round(ltp + half, 2)
    return {"ltp": ltp, "bid": bid, "ask": ask}


def synthetic_history(index: str, end: date, sessions: int, seed: int) -> list[Bar]:
    """Synthetic 5-minute bars for the demo's odds (labelled synthetic in the UI)."""
    rng = np.random.default_rng(seed)
    base = SCENARIOS[index]["prev_close"]
    level = base * 0.985
    vol5 = {"NIFTY": 0.00072, "SENSEX": 0.0007, "BANKNIFTY": 0.00085}.get(index, 0.0007)
    prof = np.array([2.6, 1.9, 1.5, 1.3, 1.2] + [1.0] * 64 + [1.15] * 6)  # busier open and close
    out: list[Bar] = []
    d = end - timedelta(days=1)
    days = []
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    for d in reversed(days):
        level += 0.15 * (base - level) + base * rng.normal(0, 0.005)  # stays near today's level
        price = level
        regime = rng.choice([-1.0, 0.0, 0.0, 1.0]) * 0.00009
        if d == days[0]:  # the day before the demo: NIFTY up, SENSEX down, so both demo signals pass the
            regime = -0.00007 if index == "SENSEX" else 0.00007  # yesterday's-direction filter (a normal day, not wide)
        quiet = 0.6 if d == days[0] else 1.0      # the demo's 'yesterday' is a calm day (no wide-range skip)
        for i in range(75):
            start = ist(d, hm("09:15")) + timedelta(minutes=5 * i)
            o = price
            c = o * (1 + rng.normal(regime, vol5 * prof[i] * quiet))
            h = max(o, c) * (1 + abs(rng.normal(0, vol5 * 0.5)))
            l = min(o, c) * (1 - abs(rng.normal(0, vol5 * 0.5)))
            out.append(Bar(start, round(o, 2), round(h, 2), round(l, 2), round(c, 2), 0.0, True, 5))
            price = c
    return out


def synthetic_vix(end: date, sessions: int, seed: int) -> list[Bar]:
    rng = np.random.default_rng(seed + 7)
    out: list[Bar] = []
    d = end - timedelta(days=1)
    days = []
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    v = 13.5
    for d in reversed(days):
        v = float(np.clip(v + rng.normal(0, 0.4), 10, 22))
        out.append(Bar(ist(d, hm("09:15")), v, v, v, v, 0.0, True, 5))
    return out


# ------------------------------------------------------------ demo stocks
# Fictional names so nobody mistakes demo moves for real ones.
DEMO_STOCK_NAMES = ["ARKAFIN", "BHAVYAUTO", "CHAKRAMET", "DHRUVTECH", "EKTAPHARMA", "FALAKPOWER", "GANGABANK",
                    "HIMALCEM", "INDRAOIL", "JYOTIFOODS", "KAVYARETAIL", "LAKSHINFRA", "MEGHATEL", "NAVYACHEM",
                    "OJASSTEEL", "PRAKASHFIN", "RAVIMOTORS", "SURYAENERGY", "TARANGIT", "UDAYPAPER",
                    "VAYUAIR", "YASHHOMES", "ZESTBEV", "AMBERGAS", "BODHIMED"]


def demo_stocks(day: date, seed: int = 21):
    """Fictional F&O stocks: (FnoStock, prev_close, per-second path, OI trend, IV) and their options."""
    from .movers import FnoStock

    rng = np.random.default_rng(seed)
    out, opts = [], []
    expiry = date(2026, 11, 23) if day == DEMO_DAY else _weekly("NIFTY", day, 3)[-1]
    n = 900000
    for i, name in enumerate(DEMO_STOCK_NAMES):
        pc = float(np.round(rng.choice([180, 420, 760, 1250, 2300, 3900]) * rng.uniform(0.85, 1.15), 1))
        final = float(rng.normal(0, 1.6))
        if i < 4:
            final = 2.4 + i * 0.7  # a few clear gainers
        elif i < 8:
            final = -(2.2 + (i - 4) * 0.6)  # and clear losers
        gap = final * rng.uniform(0.1, 0.4)
        anchors = [("09:15", pc * (1 + gap / 100)), ("10:30", pc * (1 + final * 0.55 / 100)),
                   ("12:30", pc * (1 + final * 0.85 / 100)), ("14:30", pc * (1 + final / 100)),
                   ("15:30", pc * (1 + final * 0.95 / 100))]
        path = bridge_path(anchors, pc * 0.00018, seed + i)
        oi_trend = float(rng.choice([1.0, -1.0], p=[0.65, 0.35])) * rng.uniform(0.04, 0.14)
        lot = int(max(1, round(600000 / pc / 25) * 25))
        st = FnoStock(name, f"E{i:03d}", f"F{i:03d}", f"{name}{expiry:%d%b%y}FUT".upper(), expiry, lot)
        iv = float(rng.uniform(0.22, 0.38))
        out.append((st, pc, path, oi_trend, iv))
        step = 5.0 if pc < 300 else 10.0 if pc < 1000 else 20.0 if pc < 2000 else 50.0
        mid = round(pc / step) * step
        for j in range(-10, 11):
            for opt in ("CE", "PE"):
                n += 1
                k = mid + j * step
                opts.append(Contract(f"S{n}", f"{name}{expiry:%d%b%y}{k:g}{opt}".upper(), name, expiry, float(k),
                                     opt, lot, 0.05, "NFO"))
    return out, opts


def demo_stock_daily(stocks, day: date, sessions: int = 30) -> dict:
    """Synthetic daily candles for the fictional demo stocks (their 'normal daily range' ~1.4-2.6% of price)."""
    rng = np.random.default_rng(77)
    out = {}
    for info, prev_close, *_ in stocks:
        width = float(rng.uniform(0.014, 0.026))
        rows, d, px = [], day - timedelta(days=1), float(prev_close)
        days = []
        while len(days) < sessions:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        for d in reversed(days):
            r = px * width * float(rng.uniform(0.7, 1.3))
            o = px
            c = px * (1 + float(rng.normal(0, width / 3)))
            rows.append((d, o, max(o, c) + r * 0.3, min(o, c) - r * 0.3, c))
            px = c
        out[info.name] = rows
    return out
