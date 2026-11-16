"""Opening-range breakout on the index, traded by BUYING an option.

1. Opening range = index high/low from 09:15 to 09:30 IST (the 09:15, 09:20, 09:25 bars).
2. Signal = the first COMPLETED 5-minute bar that closes above the range high (BUY CE)
   or below the range low (BUY PE). A wick is not a signal; a close exactly at the
   level is not beyond it. No signals from bars that close after 14:00 IST.
3. Stop on the index: range midpoint (default) or the far side of the range.
   Targets: 1R, 1.5R and 2R on the index. Time exit 15:00 IST.
4. Each index level is converted to an estimated premium with Black-Scholes at the
   option's own implied volatility.
5. Lots = the most lots whose loss at the stop, after all costs, fits your max loss.
Option buying only. Nothing here can sell or write an option.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import bs
from .candles import Bar
from .config import Settings
from .costs import Costs, round_trip, slip
from .instruments import Contract
from .timeutil import hm, ist

FIVE = timedelta(minutes=5)


def floor_to(x: float, step: float = 0.05) -> float:
    return round(math.floor(round(x / step, 6)) * step, 2)


def ceil_to(x: float, step: float = 0.05) -> float:
    return round(math.ceil(round(x / step, 6)) * step, 2)


def round_to(x: float, step: float = 0.05) -> float:
    return round(math.floor(x / step + 0.5) * step, 2)


@dataclass
class OpeningRange:
    high: float
    low: float
    bars: int
    expected: int
    complete: bool

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    def to_json(self) -> dict:
        return {"high": round(self.high, 2), "low": round(self.low, 2), "mid": round(self.mid, 2),
                "bars": self.bars, "expected": self.expected, "complete": self.complete}


def opening_range(bars5: list[Bar], d: date, s: Settings, now: datetime) -> OpeningRange | None:
    start, end = ist(d, hm(s.or_start)), ist(d, hm(s.or_end))
    rb = [b for b in bars5 if start <= b.start < end]
    if not rb:
        return None
    expected = int((end - start) / FIVE)
    complete = now >= end and all(b.closed for b in rb)
    return OpeningRange(max(b.h for b in rb), min(b.l for b in rb), len(rb), expected, complete)


@dataclass
class Signal:
    index: str
    side: str  # CE or PE
    bar_start: datetime
    time: datetime  # bar close time
    close: float
    or_high: float
    or_low: float

    def to_json(self) -> dict:
        return {"index": self.index, "side": self.side, "bar_start": self.bar_start.isoformat(),
                "time": self.time.isoformat(), "close": round(self.close, 2),
                "or_high": round(self.or_high, 2), "or_low": round(self.or_low, 2)}


def breakout_margin(orng: OpeningRange, s: Settings) -> float:
    """Points a 5-minute close must clear the range by (breakout_buffer x range width)."""
    return max(s.breakout_buffer, 0.0) * max(orng.high - orng.low, 0.0)


def find_signal(index: str, bars5: list[Bar], orng: OpeningRange, d: date, s: Settings,
                now: datetime, allowed: set[str] | None = None) -> tuple[str, Signal | None]:
    """('waiting' | 'signal' | 'expired', signal). `allowed`: sides the trend filter permits today
    (None = both). A breakout on a side that is not allowed is skipped and the scan carries on."""
    end, last = ist(d, hm(s.or_end)), ist(d, hm(s.last_signal_time))
    buf = breakout_margin(orng, s)
    ok = allowed if allowed is not None else {"CE", "PE"}
    for b in sorted((b for b in bars5 if b.closed and b.start >= end), key=lambda b: b.start):
        close_time = b.start + FIVE
        if close_time > last:
            return "expired", None
        if b.c > orng.high + buf and "CE" in ok:
            return "signal", Signal(index, "CE", b.start, close_time, b.c, orng.high, orng.low)
        if b.c < orng.low - buf and "PE" in ok:
            return "signal", Signal(index, "PE", b.start, close_time, b.c, orng.high, orng.low)
    if now > last:
        return "expired", None
    return "waiting", None


def index_levels(sig: Signal, orng: OpeningRange, s: Settings) -> tuple[float, dict[str, float], float]:
    """Stop, targets {'1R','1.5R','2R'} and risk in index points."""
    e = sig.close
    if sig.side == "CE":
        stop = floor_to(orng.mid if s.stop_rule == "mid" else orng.low)
        risk = e - stop
        targets = {f"{k:g}R": round_to(e + k * risk) for k in (1.0, 1.5, 2.0)}
    else:
        stop = ceil_to(orng.mid if s.stop_rule == "mid" else orng.high)
        risk = stop - e
        targets = {f"{k:g}R": round_to(e - k * risk) for k in (1.0, 1.5, 2.0)}
    return stop, targets, round(risk, 2)


@dataclass
class Quote:
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    prev_close: float = 0.0
    oi: int = 0
    prev_oi: int | None = None
    oi_chg_pct: float | None = None
    volume: float = 0.0
    ts: float = 0.0  # receive time (epoch seconds)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread_pct(self) -> float | None:
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / self.mid * 100.0
        return None

    @property
    def oi_change(self) -> int | None:
        if self.prev_oi is not None:
            return self.oi - self.prev_oi
        if self.oi_chg_pct not in (None, 0) and self.oi:
            base = self.oi / (1.0 + self.oi_chg_pct / 100.0)
            return int(round(self.oi - base))
        return None


@dataclass
class Sizing:
    lots: int
    qty: int
    loss_at_stop: float  # after costs, for the chosen lots (or 1 lot when lots == 0)
    costs_at_stop: Costs
    one_lot_loss: float
    decided_by: str


def size_position(entry_fill: float, stop_fill: float, lot: int, exch: str, s: Settings) -> Sizing:
    drop = max(entry_fill - stop_fill, 0.0)

    def loss(n: int) -> tuple[float, Costs]:
        c = round_trip(entry_fill, stop_fill, n * lot, exch, s)
        return n * lot * drop + c.total, c

    one, one_c = loss(1)
    if drop <= 0:
        return Sizing(0, 0, one, one_c, one, "the stop premium is not below the entry")
    n = min(int(s.max_loss_per_trade // (drop * lot)), s.max_lots)
    while n > 0:
        l, c = loss(n)
        if l <= s.max_loss_per_trade + 1e-9:
            decided = "your max loss per trade" if n < s.max_lots else "the max lots setting"
            return Sizing(n, n * lot, l, c, one, decided)
        n -= 1
    return Sizing(0, 0, one, one_c, one, "your max loss per trade (even 1 lot is too big)")


@dataclass
class Plan:
    index: str
    side: str
    signal_time: datetime
    entry_index: float
    stop_index: float
    stop_rule: str
    targets: dict[str, float]
    target_key: str
    target_index: float
    risk_pts: float
    exit_time: datetime
    contract: Contract
    expiry_flags: list[str]
    premium_ltp: float
    bid: float
    ask: float
    entry_fill: float
    iv: float
    iv_source: str
    delta: float
    theta_day: float
    prem_stop: float
    prem_targets: dict[str, float]
    prem_time_exit_flat: float
    exit_fill_stop: float
    exit_fill_target: float
    sizing: Sizing
    gain_at_target: float
    costs_at_target: Costs
    entry_limit: float
    max_entry_index: float
    max_entry_premium: float
    created: datetime
    notes: list[str] = field(default_factory=list)
    strikes: list[dict] = field(default_factory=list)
    best: dict | None = None
    exit_mode: str = "target"
    half_at_r: float = 1.0

    def to_json(self) -> dict:
        c = self.contract
        return {
            "index": self.index, "side": self.side, "signal_time": self.signal_time.isoformat(),
            "entry_index": round(self.entry_index, 2), "stop_index": self.stop_index,
            "stop_rule": self.stop_rule, "targets": self.targets, "target_key": self.target_key,
            "target_index": self.target_index, "risk_pts": self.risk_pts, "exit_mode": self.exit_mode,
            "half_at_r": self.half_at_r,
            "exit_time": self.exit_time.isoformat(),
            "contract": {**c.to_json(), "label": c.label(), "expiry_code": c.expiry_code},
            "expiry_flags": self.expiry_flags,
            "premium_ltp": round(self.premium_ltp, 2), "bid": round(self.bid, 2), "ask": round(self.ask, 2),
            "entry_fill": round(self.entry_fill, 2), "iv_pct": round(self.iv * 100, 2),
            "iv_source": self.iv_source, "delta": round(self.delta, 3),
            "theta_day": round(self.theta_day, 2),
            "prem_stop": round(self.prem_stop, 2),
            "prem_targets": {k: round(v, 2) for k, v in self.prem_targets.items()},
            "prem_time_exit_flat": round(self.prem_time_exit_flat, 2),
            "exit_fill_stop": round(self.exit_fill_stop, 2),
            "exit_fill_target": round(self.exit_fill_target, 2),
            "lots": self.sizing.lots, "qty": self.sizing.qty, "lot": c.lot,
            "loss_at_stop": round(self.sizing.loss_at_stop, 2),
            "one_lot_loss": round(self.sizing.one_lot_loss, 2),
            "costs_at_stop": self.sizing.costs_at_stop.to_json(),
            "sized_by": self.sizing.decided_by,
            "gain_at_target": round(self.gain_at_target, 2),
            "costs_at_target": self.costs_at_target.to_json(),
            "entry_limit": self.entry_limit, "max_entry_index": self.max_entry_index,
            "max_entry_premium": round(self.max_entry_premium, 2),
            "notes": self.notes, "strikes": self.strikes, "best": self.best,
        }


def make_plan(sig: Signal, orng: OpeningRange, contract: Contract, q: Quote, spot_now: float,
              now: datetime, s: Settings, exch: str, expiry_flags: list[str]) -> Plan:
    stop, targets, risk = index_levels(sig, orng, s)
    tkey = f"{s.target_r:g}R"
    r = s.risk_free_rate_pct / 100.0
    K, kind = contract.strike, contract.opt
    T_now = bs.years_to_expiry(now, contract.expiry)
    exit_dt = ist(sig.time.date(), hm(s.time_exit))
    T_exit = bs.years_to_expiry(max(exit_dt, now), contract.expiry)
    notes: list[str] = []

    market = q.mid if q.mid > 0 else q.ltp
    iv = bs.implied_vol(market, spot_now, K, T_now, r, kind) if market > 0 else None
    iv_source = "from the option's live price"
    if iv is None:
        iv = s.default_iv_pct / 100.0
        iv_source = f"default {s.default_iv_pct:g}% (could not read it from the live price)"
        notes.append("Implied volatility could not be read from the live price; premiums are rougher estimates.")
    model_now = bs.price(spot_now, K, T_now, r, iv, kind)
    base = market if market > 0 else model_now

    def prem_at(level: float, T: float = T_now) -> float:
        return max(contract.tick, base + bs.price(level, K, T, r, iv, kind) - model_now)

    tick = contract.tick or 0.05
    sl = slip(tick, s)
    if q.ask > 0 and q.bid > 0:
        entry_fill = q.ask + sl
        half_spread = (q.ask - q.bid) / 2.0
    else:
        entry_fill = base * (1 + s.slippage_pct_no_quote / 100.0)
        half_spread = base * s.slippage_pct_no_quote / 100.0
        notes.append("No live bid/ask: fills assume a slippage allowance.")
    prem_stop = prem_at(stop)
    prem_targets = {k: prem_at(v) for k, v in targets.items()}
    exit_fill_stop = max(tick, prem_stop - half_spread - sl)
    exit_fill_target = max(tick, prem_targets[tkey] - half_spread - sl)

    sizing = size_position(entry_fill, exit_fill_stop, contract.lot, exch, s)
    qty = sizing.qty if sizing.lots else contract.lot
    ct = round_trip(entry_fill, exit_fill_target, qty, exch, s)
    gain = qty * (exit_fill_target - entry_fill) - ct.total

    # Entry zone: the furthest the index may run before the rest of the move to the
    # target pays less than min_reward_risk against the stop. Beyond it: don't chase.
    m = max(s.min_reward_risk, 0.1)
    chase = (targets[tkey] + m * stop) / (1.0 + m)
    chase = floor_to(chase) if sig.side == "CE" else ceil_to(chase)
    max_entry_premium = prem_at(chase) + half_spread + sl
    entry_limit = round_to(entry_fill, tick)

    return Plan(
        index=sig.index, side=sig.side, signal_time=sig.time, entry_index=sig.close,
        stop_index=stop, stop_rule=s.stop_rule, targets=targets, target_key=tkey,
        target_index=targets[tkey], risk_pts=risk, exit_time=exit_dt, contract=contract,
        expiry_flags=list(expiry_flags), premium_ltp=q.ltp, bid=q.bid, ask=q.ask,
        entry_fill=round(entry_fill, 2), iv=iv, iv_source=iv_source,
        delta=bs.delta(spot_now, K, T_now, r, iv, kind),
        theta_day=bs.theta_per_day(spot_now, K, T_now, r, iv, kind),
        prem_stop=prem_stop, prem_targets=prem_targets,
        prem_time_exit_flat=prem_at(spot_now, T_exit),
        exit_fill_stop=exit_fill_stop, exit_fill_target=exit_fill_target,
        sizing=sizing, gain_at_target=gain, costs_at_target=ct, entry_limit=entry_limit,
        max_entry_index=chase, max_entry_premium=round_to(max_entry_premium, tick),
        created=now, notes=notes, exit_mode=s.exit_mode, half_at_r=s.half_at_r,
    )


OFFSET_LABEL = {-2: "2 ITM", -1: "1 ITM", 0: "ATM", 1: "1 OTM", 2: "2 OTM"}


def compare_strikes(sig: Signal, orng: OpeningRange, u, expiry, quotes: dict, spot_now: float, now: datetime,
                    s: Settings, exch: str, flags: list[str], stock: bool = False) -> tuple[list[dict], dict | None]:
    """Compare 2 ITM .. 2 OTM for the signal's side and pick the best fit by a stated rule.

    Gain at the target and loss at the stop are after all charges and include the time
    decay of an average holding time (half the time left to the 15:00 exit), so strikes
    that bleed faster are penalised. Eligible: delta 0.40-0.70, spread within your limit,
    premium within your range, a live quote, and at least 1 lot inside your max loss.
    Best fit = the highest gain-to-loss ratio among eligible strikes; ratios within 0.05 of the
    best count as a tie, and ties go to the delta nearest 0.55, then the tighter spread.
    """
    ks = u.strikes(expiry)
    if not ks:
        return [], None
    atm = u.atm(sig.close, expiry)
    i = ks.index(atm)
    exit_dt = ist(sig.time.date(), hm(s.time_exit))
    hold_days = max((exit_dt - now).total_seconds(), 0) / 2.0 / 86400.0
    rows = []
    for off in (-2, -1, 0, 1, 2):
        j = i + off if sig.side == "CE" else i - off  # ITM = lower strikes for calls, higher for puts
        if not 0 <= j < len(ks):
            continue
        c = u.contract(expiry, ks[j], sig.side)
        if c is None:
            continue
        q = quotes.get(c.token) or Quote()
        p = make_plan(sig, orng, c, q, spot_now, now, s, exch, flags)
        lots = p.sizing.lots
        qty = p.sizing.qty if lots else c.lot
        decay = abs(p.theta_day) * hold_days * qty
        loss = (p.sizing.loss_at_stop if lots else p.sizing.one_lot_loss) + decay
        gain = p.gain_at_target - decay
        sp = q.spread_pct
        reasons = []
        if q.ltp <= 0:
            reasons.append("no live quote")
        if not (0.40 <= abs(p.delta) <= 0.70):
            reasons.append(f"delta {abs(p.delta):.2f} outside 0.40-0.70")
        if sp is not None and sp > s.max_spread_pct:
            reasons.append(f"spread {sp:.1f}%")
        if not stock and not (s.min_premium(sig.index) <= p.entry_fill <= s.max_premium(sig.index)):
            reasons.append("premium outside your range")
        if lots == 0:
            reasons.append("1 lot is over your max loss")
        rows.append({"label": OFFSET_LABEL[off], "strike": c.strike, "token": c.token, "opt": c.opt,
                     "premium": round(p.entry_fill, 2), "delta": round(p.delta, 2),
                     "spread_pct": round(sp, 2) if sp is not None else None, "lots": lots, "qty": qty,
                     "loss": round(loss, 0), "gain": round(gain, 0),
                     "rr": round(gain / loss, 2) if loss > 0 else 0.0,
                     "theta_day": round(p.theta_day, 2), "ok": not reasons, "why_not": reasons,
                     "best": False})
    eligible = [r for r in rows if r["ok"]]
    if not eligible:
        return rows, None
    top = max(r["rr"] for r in eligible)
    near = [r for r in eligible if r["rr"] >= top - 0.05]
    best = min(near, key=lambda r: (abs(abs(r["delta"]) - 0.55),
                                    r["spread_pct"] if r["spread_pct"] is not None else 99))
    best["best"] = True
    sp_txt = f"spread {best['spread_pct']:.1f}%, " if best["spread_pct"] is not None else ""
    best_info = dict(best)
    best_info["reason"] = (
        f"{sig.index} {best['strike']:,.0f} {best['opt']} ({best['label']}) at about ₹{best['premium']:,.2f}: "
        f"delta {abs(best['delta']):.2f}, so it moves about ₹{abs(best['delta']):.2f} for each "
        f"{'₹1 move in ' + sig.index if stock else sig.index + ' point'}; "
        f"{sp_txt}{best['lots']} lot{'s' if best['lots'] != 1 else ''}. After charges and time decay it makes about "
        f"₹{best['gain']:,.0f} at the target against about ₹{best['loss']:,.0f} at the stop ({best['rr']:.2f} to 1).")
    return rows, best_info
