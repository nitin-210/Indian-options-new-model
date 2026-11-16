"""The odds: the same rule replayed on past sessions, plus a Monte Carlo replay
of the rest of today's session (the repo's "Time Machine" idea, on index bars).

Index outcomes come from real 5-minute index history. Option P&L is an
ESTIMATE: Angel One does not provide prices for expired option contracts, so
premiums are rebuilt with Black-Scholes using India VIX as the volatility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from . import bs
from .candles import Bar
from .config import Settings
from .costs import round_trip
from .strategy import FIVE, OpeningRange, Signal, breakout_margin, index_levels, size_position
from .timeutil import AUCTION_FROM, INDEX_AUCTION, IST, hm, ist

BARS_PER_SESSION = 72  # 09:15 .. 15:10 five-minute bars (the index stops at 15:15 since 3 Aug 2026)
LAST_BAR = "15:10"


def weekly_expiry_for(index: str, d: date) -> date:
    """Approximate weekly expiry used in history (holiday shifts ignored).

    NIFTY: Thursday until 31 Aug 2025, Tuesday from 1 Sep 2025.
    SENSEX: Friday until 2024, Tuesday Jan-Aug 2025, Thursday from 1 Sep 2025.
    On an expiry day the rule uses the next week's expiry, as the live app does.
    """
    if index == "BANKNIFTY":
        return monthly_expiry_for(index, d)
    if index == "NIFTY":
        wd = 3 if d < date(2025, 9, 1) else 1
    else:
        wd = 4 if d < date(2025, 1, 1) else (1 if d < date(2025, 9, 1) else 3)
    days = (wd - d.weekday()) % 7
    e = d + timedelta(days=days)
    if e == d:
        e += timedelta(days=7)
    return e


def monthly_expiry_for(index: str, d: date) -> date:
    """NIFTY BANK has monthly expiries only (weekly ones ended Nov 2024): the last Thursday of the month until
    31 Aug 2025, the last Tuesday from 1 Sep 2025 (holiday shifts ignored). On an expiry day: next month's."""
    def last_wd(y: int, m: int) -> date:
        wd = 3 if date(y, m, 1) < date(2025, 9, 1) else 1
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        last = nxt - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - wd) % 7)
    e = last_wd(d.year, d.month)
    if e <= d:
        y, m = (d.year + (d.month == 12), d.month % 12 + 1)
        e = last_wd(y, m)
    return e


def full_sessions(bars: list[Bar]) -> dict[date, list[Bar]]:
    """Days with every 5-minute bar from 09:15 to 15:10 IST (72 bars); later bars are ignored.

    Since 3 Aug 2026 continuous trading in F&O stocks stops at 15:15 (then a closing auction
    to 15:35), so NIFTY and SENSEX have no bars from 15:15 on. The rule exits by 15:00, so
    09:15-15:10 covers everything it uses, before and after the change. Duplicates count once.
    """
    first, last = hm("09:15"), hm(LAST_BAR)
    by: dict[date, dict[datetime, Bar]] = {}
    for b in bars:
        t = b.start.astimezone(IST)
        if first <= t.time() <= last and t.minute % 5 == 0 and t.second == 0:
            by.setdefault(t.date(), {})[t] = b
    out = {d: [m[k] for k in sorted(m)] for d, m in by.items() if len(m) == BARS_PER_SESSION}
    return dict(sorted(out.items()))


def describe_day(bars: list[Bar], d: date) -> str:
    """'60 bars, 09:15-15:35, missing 15:15, 15:20' — why a day is not a full session."""
    ts = sorted({b.start.astimezone(IST) for b in bars if b.start.astimezone(IST).date() == d})
    if not ts:
        return "no bars"
    want = [ist(d, hm("09:15")) + i * FIVE for i in range(BARS_PER_SESSION)]
    have = set(ts)
    missing = [t.strftime("%H:%M") for t in want if t not in have]
    odd = [t.strftime("%H:%M:%S") for t in ts if t.minute % 5 or t.second][:3]
    out = f"{len(ts)} bars, {ts[0]:%H:%M}-{ts[-1]:%H:%M}"
    if missing:
        out += ", missing " + ", ".join(missing[:4]) + (f" +{len(missing) - 4} more" if len(missing) > 4 else "")
    if odd:
        out += ", off-grid times " + ", ".join(odd)
    return out


def verdict_line(target: int, stop: int, r_mult: float) -> str:
    hits = target + stop
    if hits < 5:
        return "Too few trades hit the stop or the target to judge."
    share = target / hits
    be = 1.0 / (1.0 + r_mult)
    diff = (share - be) * 100.0
    side = "target" if diff > 0 else "stop"
    if abs(diff) < 5:
        return ("Close to a coin flip" if r_mult == 1 else "Close to break-even") + \
            f": the target came first in {share:.0%} of decided trades; break-even at {r_mult:g}R is {be:.0%}."
    word = "Leans toward the" if abs(diff) < 15 else "Strongly toward the"
    return f"{word} {side}: target first in {share:.0%} of decided trades vs {be:.0%} needed at {r_mult:g}R."


MIN_SESSION_MINUTES = 180   # a regular day spans 09:15-15:25; the Diwali Muhurat session is about an hour


def regular_sessions(bars) -> dict[date, list]:
    """Each regular session's bars from 09:15 to 15:25 IST, oldest first.

    Special short sessions are left out: the one-hour Muhurat session on Diwali (e.g. Sun 8 Nov 2026, 14:20-15:25)
    is not a normal "yesterday" - using it as one flips yesterday's direction and hides a wide-range day.
    """
    first, last = hm("09:15"), hm("15:25")
    by: dict[date, list] = {}
    for b in bars:
        t = b.start.astimezone(IST)
        if first <= t.time() <= last:
            by.setdefault(t.date(), []).append(b)
    out = {}
    for d in sorted(by):
        v = sorted(by[d], key=lambda b: b.start)
        if (v[-1].start - v[0].start).total_seconds() >= MIN_SESSION_MINUTES * 60:
            out[d] = v
    return out


def continuous(d: date, bars: list) -> list:
    """A day's bars without the closing-auction candles (from 15:15 since 3 Aug 2026). During the auction the
    index is calculated from indicative prices and Angel's 15:20-15:25 candles can carry wild highs and lows (on
    6 Nov 2026 NIFTY's 15:20 candle spanned 572 points); they are not traded prices, so they stay out of the
    day's high and low. The day's close (the official close) still comes from the last candle."""
    if d < AUCTION_FROM:
        return bars
    keep = [b for b in bars if b.start.astimezone(IST).time() < INDEX_AUCTION]
    return keep or bars


def day_open_close(bars) -> dict[date, tuple[float, float]]:
    """Each regular day's first open (from 09:15) and last close (to 15:25)."""
    return {d: (v[0].o, v[-1].c) for d, v in regular_sessions(bars).items()}


def direction(o: float, c: float) -> str | None:
    return "CE" if c > o else ("PE" if c < o else None)


def yesterday_sides(bars) -> dict[date, str]:
    """For each day in the data: the side allowed by the day before (up -> CE, down -> PE)."""
    oc = day_open_close(bars)
    ds = sorted(oc)
    out = {}
    for i in range(1, len(ds)):
        side = direction(*oc[ds[i - 1]])
        if side:
            out[ds[i]] = side
    return out


def yesterday_for(bars, today: date) -> dict | None:
    """The last trading day before `today`: its date, open, close and the side it allows."""
    oc = day_open_close(b for b in bars if b.start.astimezone(IST).date() < today)
    if not oc:
        return None
    d = max(oc)
    o, c = oc[d]
    return {"date": d, "open": o, "close": c, "side": direction(o, c)}


def yesterday_range_atr(bars, today: date) -> float | None:
    """Yesterday's high-low range divided by the average range of the 14 sessions up to and including yesterday
    (research name y_range_atr). Above 1.2 = a wide-range day; the tested model skips the next day."""
    days = {d: [max(b.h for b in continuous(d, v)), min(b.l for b in continuous(d, v))]
            for d, v in regular_sessions(b for b in bars if b.start.astimezone(IST).date() < today).items()}
    ds = sorted(days)
    if len(ds) < 14:
        return None
    rng = [days[d][0] - days[d][1] for d in ds[-14:]]
    avg = sum(rng) / 14
    return rng[-1] / avg if avg > 0 else None


def allowed_sides(s: Settings, d: date, prev: dict[date, str] | None) -> set[str]:
    if s.trend_filter != "yesterday" or prev is None:
        return {"CE", "PE"}
    side = prev.get(d)
    return {side} if side else set()


def hold_verdict(n: int, stopped: int, trades: list[dict]) -> str:
    """Verdict for the stop-or-time-exit rule (no fixed target)."""
    if n < 20:
        return "Too few trades to judge."
    avg = sum(t["r_after_costs"] for t in trades) / n
    word = "comes out ahead" if avg > 0.03 else ("loses money" if avg < -0.03 else "is about break-even")
    return (f"Held to the time exit in {(n - stopped) / n:.0%} of trades, stopped out in {stopped / n:.0%}; "
            f"after charges the rule {word} ({avg:+.2f}R a trade, estimate).")


def half_verdict(n: int, halves: int, stopped: int, trades: list[dict], at_r: float) -> str:
    if n < 20:
        return "Too few trades to judge."
    avg = sum(t["r_after_costs"] for t in trades) / n
    word = "comes out ahead" if avg > 0.03 else ("loses money" if avg < -0.03 else "is about break-even")
    return (f"Half sold at +{at_r:g}R in {halves / n:.0%} of trades, full stop in {stopped / n:.0%}; after charges "
            f"the rule {word} ({avg:+.2f}R a trade, estimate; needs at least 2 lots to split).")


@dataclass
class Ctx:
    index: str
    lot: int
    step: float
    exch: str


def _option_estimate(ctx: Ctx, s: Settings, d: date, side: str, entry: float, stop: float,
                     exit_px: float, t_entry: datetime, t_exit: datetime, iv: float) -> dict:
    r = s.risk_free_rate_pct / 100.0
    exp = weekly_expiry_for(ctx.index, d)
    atm = round(entry / ctx.step) * ctx.step
    K = atm if s.strike_mode == "atm" else (atm - ctx.step if side == "CE" else atm + ctx.step)
    T0, T1 = bs.years_to_expiry(t_entry, exp), bs.years_to_expiry(t_exit, exp)
    slip = s.slippage_pct_no_quote / 100.0
    p0 = bs.price(entry, K, T0, r, iv, side)
    entry_fill = p0 * (1 + slip)
    stop_fill = bs.price(stop, K, T0, r, iv, side) * (1 - slip)
    exit_fill = max(0.05, bs.price(exit_px, K, T1, r, iv, side) * (1 - slip))
    sz = size_position(entry_fill, stop_fill, ctx.lot, ctx.exch, s)
    lots = sz.lots or 1
    qty = lots * ctx.lot
    c = round_trip(entry_fill, exit_fill, qty, ctx.exch, s)
    pnl = qty * (exit_fill - entry_fill) - c.total
    planned = sz.loss_at_stop if sz.lots else sz.one_lot_loss
    return {"premium": round(p0, 2), "exit_premium": round(exit_fill, 2), "lots": sz.lots,
            "pnl": round(pnl, 2), "r_after_costs": round(pnl / planned, 3) if planned > 0 else 0.0,
            "expiry": exp.isoformat(), "strike": K}


def backtest(sessions: dict[date, list[Bar]], s: Settings, ctx: Ctx,
             vix: dict[date, list[Bar]] | None = None, keep: int | None = 250,
             prev: dict[date, str] | None = None) -> dict:
    """prev: yesterday_sides(raw bars); without it the trend filter is not applied."""
    tkey = f"{s.target_r:g}R"
    hold = s.exit_mode in ("hold", "half", "trail")
    half_mode = s.exit_mode == "half"
    trail_mode = s.exit_mode == "trail"
    trades = []
    no_signal = 0
    for d, bars in sessions.items():
        allowed = allowed_sides(s, d, prev)
        or_end = ist(d, hm(s.or_end))
        rb = [b for b in bars if b.start < or_end]
        if not rb:
            continue
        orng = OpeningRange(max(b.h for b in rb), min(b.l for b in rb), len(rb), len(rb), True)
        last = ist(d, hm(s.last_signal_time))
        texit = ist(d, hm(s.time_exit))
        buf = breakout_margin(orng, s)
        sig, si = None, -1
        for i, b in enumerate(bars):
            if b.start < or_end:
                continue
            if b.start + FIVE > last:
                break
            if b.c > orng.high + buf and "CE" in allowed:
                sig, si = Signal(ctx.index, "CE", b.start, b.start + FIVE, b.c, orng.high, orng.low), i
                break
            if b.c < orng.low - buf and "PE" in allowed:
                sig, si = Signal(ctx.index, "PE", b.start, b.start + FIVE, b.c, orng.high, orng.low), i
                break
        if sig is None:
            no_signal += 1
            continue
        stop, targets, risk = index_levels(sig, orng, s)
        tgt = targets[tkey]
        if risk <= 0:
            continue
        outcome, exit_px, exit_t = "neither", None, texit
        last_close = sig.close
        cur_stop, half = stop, None
        best = sig.close
        half_lvl = sig.close + (s.half_at_r * risk if sig.side == "CE" else -s.half_at_r * risk)
        for b in bars[si + 1:]:
            if b.start + FIVE > texit:
                break
            last_close = b.c
            if sig.side == "CE":
                if b.l <= cur_stop:
                    outcome, exit_px, exit_t = "stop", min(cur_stop, b.o), b.start + FIVE
                    break
                if not hold and b.h >= tgt:
                    outcome, exit_px, exit_t = "target", tgt, b.start + FIVE
                    break
                if half_mode and half is None and b.h >= half_lvl:
                    half, cur_stop = (half_lvl, b.start + FIVE), max(cur_stop, sig.close)
                if trail_mode:
                    best = max(best, b.h)
                    if best - sig.close >= risk:
                        cur_stop = max(cur_stop, best - risk)
            else:
                if b.h >= cur_stop:
                    outcome, exit_px, exit_t = "stop", max(cur_stop, b.o), b.start + FIVE
                    break
                if not hold and b.l <= tgt:
                    outcome, exit_px, exit_t = "target", tgt, b.start + FIVE
                    break
                if half_mode and half is None and b.l <= half_lvl:
                    half, cur_stop = (half_lvl, b.start + FIVE), min(cur_stop, sig.close)
                if trail_mode:
                    best = min(best, b.l)
                    if sig.close - best >= risk:
                        cur_stop = min(cur_stop, best + risk)
        if exit_px is None:
            exit_px = last_close
        sign = 1 if sig.side == "CE" else -1
        r_idx = sign * (exit_px - sig.close) / risk
        iv = s.default_iv_pct / 100.0
        vix_src = "default"
        if vix and d in vix:
            prior = [v for v in vix[d] if v.start <= sig.bar_start]
            if prior:
                iv, vix_src = prior[-1].c / 100.0, "India VIX"
        opt = _option_estimate(ctx, s, d, sig.side, sig.close, stop, exit_px, sig.time, exit_t, iv)
        if half is not None:  # B: half sold at half_at_r, the rest at the final exit (half quantities, estimate)
            o1 = _option_estimate(ctx, s, d, sig.side, sig.close, stop, half[0], sig.time, half[1], iv)
            r_idx = 0.5 * s.half_at_r + 0.5 * r_idx
            for k in ("pnl", "r_after_costs"):
                opt[k] = round(0.5 * (opt[k] + o1[k]), 3)
            opt["exit_premium"] = round(0.5 * (opt["exit_premium"] + o1["exit_premium"]), 2)
            outcome = "half+" + ("stop" if outcome == "stop" else "time")
        trades.append({"date": d.isoformat(), "side": sig.side, "time": sig.time.strftime("%H:%M"),
                       "entry": round(sig.close, 2), "stop": stop, "target": tgt, "outcome": outcome,
                       "exit": round(exit_px, 2), "r_index": round(r_idx, 3), "iv_source": vix_src,
                       **opt})
    n = len(trades)
    cnt = {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "neither")}
    cnt["half"] = sum(1 for t in trades if t["outcome"].startswith("half"))
    summary = {
        "index": ctx.index, "sessions": len(sessions), "fired": n, "no_signal": no_signal,
        "ce": sum(1 for t in trades if t["side"] == "CE"), "pe": sum(1 for t in trades if t["side"] == "PE"),
        **cnt,
        "avg_r_index": round(sum(t["r_index"] for t in trades) / n, 3) if n else None,
        "avg_r_after_costs": round(sum(t["r_after_costs"] for t in trades) / n, 3) if n else None,
        "win_rate_after_costs": round(sum(1 for t in trades if t["pnl"] > 0) / n, 3) if n else None,
        "total_pnl_est": round(sum(t["pnl"] for t in trades), 0) if n else 0,
        "first": min(sessions).isoformat() if sessions else None,
        "last": max(sessions).isoformat() if sessions else None,
        "target_r": s.target_r, "stop_rule": s.stop_rule, "exit_mode": s.exit_mode,
        "breakout_buffer": s.breakout_buffer, "trend_filter": s.trend_filter if prev is not None else "none",
        "half_at_r": s.half_at_r,
        "verdict": (half_verdict(n, cnt["half"], cnt["stop"], trades, s.half_at_r) if half_mode
                    else hold_verdict(n, cnt["stop"], trades) if hold
                    else verdict_line(cnt["target"], cnt["stop"], s.target_r)),
        "note": "Index outcomes are real history. Option P&L is a Black-Scholes estimate "
                "(India VIX as volatility) because expired option prices are not available.",
    }
    return {"summary": summary, "trades": trades[-keep:] if keep else trades}


# -------------------------------------------------------------- Monte Carlo


def build_pool(sessions: dict[date, list[Bar]]) -> np.ndarray:
    """(sessions, 72, 4): each bar's open/high/low/close over the previous bar's close."""
    rows = []
    for bars in sessions.values():
        if len(bars) < BARS_PER_SESSION:
            continue
        arr = np.empty((BARS_PER_SESSION, 4))
        prev = bars[0].o
        for i, b in enumerate(bars[:BARS_PER_SESSION]):
            base = prev if i else b.o
            arr[i] = (b.o / base, b.h / base, b.l / base, b.c / base)
            prev = b.c
        rows.append(arr)
    return np.array(rows) if rows else np.empty((0, BARS_PER_SESSION, 4))


def _ncdf(x: np.ndarray) -> np.ndarray:
    # Abramowitz-Stegun 7.1.26 erf approximation (max error ~1.5e-7)
    z = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * np.exp(-z * z)
    erf = np.sign(x) * y
    return 0.5 * (1.0 + erf)


def bs_vec(S: np.ndarray, K: float, T: np.ndarray, r: float, sigma: float, kind: str) -> np.ndarray:
    T = np.maximum(T, bs.MIN_T)
    sq = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = np.exp(-r * T)
    if kind == "CE":
        return S * _ncdf(d1) - K * disc * _ncdf(d2)
    return K * disc * _ncdf(-d2) - S * _ncdf(-d1)


def monte_carlo(pool: np.ndarray, plan_side: str, entry: float, stop: float, target: float,
                signal_time: datetime, s: Settings, option: dict | None = None,
                window: int = 2) -> dict:
    """Replay the rest of the session `mc_replays` times from past bars at the same time of day.

    Each step draws a past 5-minute bar from the same time of day (+-window bars)
    and scales it to the current price. Stop is checked before target inside a
    bar (the cautious choice). A gap through the stop exits at the bar's open.
    option: {"strike","expiry","iv","entry_fill","lot","lots","exch","slip_pct"} for P&L estimates.
    """
    n_sess = pool.shape[0]
    if n_sess < 20:
        return {"error": f"Need at least 20 full past sessions for the replay; have {n_sess}."}
    d = signal_time.astimezone(IST).date()
    day_open = ist(d, hm("09:15"))
    start_bar = int((signal_time - day_open) / FIVE)  # first bar after the signal bar
    end_bar = int((ist(d, hm(s.time_exit)) - day_open) / FIVE)  # bars that end by the exit time
    nb = end_bar - start_bar
    if nb <= 0:
        return {"error": "No time left before the time exit."}
    rng = np.random.default_rng(s.mc_seed)
    N = int(s.mc_replays)
    prev = np.full(N, float(entry))
    state = np.zeros(N, dtype=np.int8)  # 0 open, 1 target, -1 stop
    exit_px = np.full(N, np.nan)
    exit_bar = np.full(N, nb - 1)
    closes = np.empty((N, nb))
    ce = plan_side == "CE"
    use_target = s.exit_mode not in ("hold", "trail")
    for j in range(nb):
        b = start_bar + j
        bb = np.clip(b + rng.integers(-window, window + 1, N), 1, BARS_PER_SESSION - 1)
        ss = rng.integers(0, n_sess, N)
        r = pool[ss, bb]
        o, h, l, c = prev * r[:, 0], prev * r[:, 1], prev * r[:, 2], prev * r[:, 3]
        open_ = state == 0
        if ce:
            s_hit = open_ & (l <= stop)
            t_hit = open_ & ~s_hit & (h >= target) & use_target
            exit_px[s_hit] = np.minimum(stop, o[s_hit])
        else:
            s_hit = open_ & (h >= stop)
            t_hit = open_ & ~s_hit & (l <= target) & use_target
            exit_px[s_hit] = np.maximum(stop, o[s_hit])
        exit_px[t_hit] = target
        state[s_hit], state[t_hit] = -1, 1
        exit_bar[s_hit | t_hit] = j
        closes[:, j] = c
        prev = c
    still = state == 0
    exit_px[still] = closes[still, -1]
    risk = abs(entry - stop)
    sign = 1 if ce else -1
    r_idx = sign * (exit_px - entry) / risk
    final = closes[:, -1]
    green = (state == 1) | (still & (sign * (final - entry) > 0))
    fan = {f"p{p}": np.round(np.percentile(closes, p, axis=0), 2).tolist() for p in (5, 25, 50, 75, 95)}
    times = [int((day_open + FIVE * (start_bar + j + 1)).timestamp()) + 19800 for j in range(nb)]
    out = {
        "replays": N, "seed": s.mc_seed, "pool_sessions": n_sess, "bars": nb,
        "side": plan_side, "entry": round(float(entry), 2), "stop": round(float(stop), 2),
        "target": round(float(target), 2),
        "target_first": round(float((state == 1).mean()) * 100, 1),
        "stop_first": round(float((state == -1).mean()) * 100, 1),
        "neither": round(float(still.mean()) * 100, 1),
        "green": round(float(green.mean()) * 100, 1),
        "final_p5": round(float(np.percentile(final, 5)), 2),
        "final_p50": round(float(np.percentile(final, 50)), 2),
        "final_p95": round(float(np.percentile(final, 95)), 2),
        "avg_r_index": round(float(r_idx.mean()), 3),
        "fan": fan, "fan_times": times,
        "verdict": verdict_line(int((state == 1).sum()), int((state == -1).sum()), s.target_r),
        "exit_mode": s.exit_mode,
    }
    if not use_target:
        out["verdict"] = (f"Stopped out in {out['stop_first']:.0f}% of replays; the rest are held to the time exit "
                          f"(average {out['avg_r_index']:+.2f}R on the index).")
    if use_target and still.mean() > 0.4:
        out["verdict_note"] = f"{still.mean():.0%} of replays hit neither by the time exit; the price then decides those."
    if option:
        rr = s.risk_free_rate_pct / 100.0
        exp_dt = datetime.combine(date.fromisoformat(option["expiry"]), hm("15:30"), tzinfo=IST)
        t_exit = np.array([(signal_time + FIVE * (k + 1)).timestamp() for k in exit_bar])
        T = np.maximum((exp_dt.timestamp() - t_exit) / bs.YEAR_S, bs.MIN_T)
        prem = bs_vec(exit_px, option["strike"], T, rr, option["iv"], plan_side)
        exit_fill = np.maximum(0.05, prem * (1 - option.get("slip_pct", 0.5) / 100.0))
        qty = max(option.get("lots") or 1, 1) * option["lot"]
        ef = option["entry_fill"]
        buy_turn, sell_turn = ef * qty, exit_fill * qty
        txn = s.txn_pct(option["exch"]) / 100.0 * (buy_turn + sell_turn)
        sebi = s.sebi_per_crore / 1e7 * (buy_turn + sell_turn)
        brokerage = 2 * s.brokerage_per_order
        costs = (brokerage + s.stt_sell_pct / 100.0 * sell_turn + txn + sebi
                 + s.stamp_buy_pct / 100.0 * buy_turn + s.gst_pct / 100.0 * (brokerage + txn + sebi))
        pnl = qty * (exit_fill - ef) - costs
        planned = option.get("planned_loss") or float(np.percentile(-pnl, 99)) or 1.0
        r_after = pnl / planned
        hist, edges = np.histogram(np.clip(r_after, -2, 4), bins=24, range=(-2, 4))
        out["option"] = {
            "qty": qty, "avg_pnl": round(float(pnl.mean()), 0), "median_pnl": round(float(np.median(pnl)), 0),
            "p5_pnl": round(float(np.percentile(pnl, 5)), 0), "p95_pnl": round(float(np.percentile(pnl, 95)), 0),
            "prob_profit": round(float((pnl > 0).mean()) * 100, 1),
            "avg_r_after_costs": round(float(r_after.mean()), 3),
            "hist": hist.tolist(), "hist_edges": np.round(edges, 2).tolist(),
            "note": "Option P&L is a Black-Scholes estimate at today's implied volatility.",
        }
    return out
