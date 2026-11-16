"""Risk check: PASS or BLOCK, with a plain reason for every line.

A PASS only means the signal fits your rules. It is not advice and not an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .strategy import Plan, Quote
from .timeutil import fmt_both, hm, ist


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | BLOCK
    message: str

    def to_json(self) -> dict:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass
class RiskContext:
    now: datetime
    feed_live: bool
    feed_reason: str
    market_open: bool
    trades_today: int
    realized_today: float
    open_risk: float
    spot_now: float
    quote: Quote
    index_expiry_today: bool
    demo: bool = False


@dataclass
class RiskResult:
    verdict: str
    checks: list[Check] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "checks": [c.to_json() for c in self.checks],
                "blocks": [c.message for c in self.checks if c.status == "BLOCK"]}


def rupees(x: float) -> str:
    return f"₹{x:,.0f}"


def check(plan: Plan, ctx: RiskContext, s: Settings) -> RiskResult:
    out: list[Check] = []

    def add(name, ok, msg_ok, msg_bad, warn=False):
        out.append(Check(name, "PASS" if ok else ("WARN" if warn else "BLOCK"), msg_ok if ok else msg_bad))

    # 1. Data must be live
    add("Data", ctx.feed_live, "Prices are live.",
        f"Data is not live ({ctx.feed_reason}). Never act on stale prices.")

    # 2. Max loss was chosen by you
    if not s.max_loss_confirmed:
        out.append(Check("Your limits", "WARN" if ctx.demo else "BLOCK",
                         f"Max loss per trade is still the default {rupees(s.max_loss_per_trade)}. "
                         "Set your own in Settings."))

    # 3. Max loss per trade
    sz = plan.sizing
    add("Max loss", sz.lots > 0,
        f"{sz.lots} lot{'s' if sz.lots != 1 else ''} ({sz.qty}) loses about {rupees(sz.loss_at_stop)} "
        f"at the stop after costs, within your {rupees(s.max_loss_per_trade)}.",
        f"Even 1 lot would lose about {rupees(sz.one_lot_loss)} at the stop after costs, "
        f"over your {rupees(s.max_loss_per_trade)} max loss.")

    # 4. Daily loss limit
    used = max(0.0, -ctx.realized_today) + ctx.open_risk
    room = s.daily_loss_limit - used
    this = sz.loss_at_stop if sz.lots else sz.one_lot_loss
    add("Daily loss", this <= room + 1e-9,
        f"Today: {rupees(used)} used of {rupees(s.daily_loss_limit)}; this trade could add {rupees(this)}.",
        f"This trade could lose {rupees(this)}, but only {rupees(max(room, 0))} is left of today's "
        f"{rupees(s.daily_loss_limit)} limit.")

    # 5. Trades a day
    add("Trades today", ctx.trades_today < s.max_trades_per_day,
        f"{ctx.trades_today} of {s.max_trades_per_day} trades used today.",
        f"You've already had {ctx.trades_today} of {s.max_trades_per_day} trades today.")

    # 6. Time window
    d = plan.signal_time.date()
    first_ok = ist(d, hm(s.or_end)) + timedelta(minutes=5)
    last_ok = ist(d, hm(s.last_signal_time))
    in_window = first_ok <= plan.signal_time <= last_ok and ctx.now <= last_ok and ctx.market_open
    add("Time window", in_window,
        f"Inside the signal window (until {fmt_both(last_ok)}).",
        "Outside the signal window: no new trades after "
        f"{fmt_both(last_ok)}, or the market is closed.")

    # 7. Signal age
    age = (ctx.now - plan.signal_time).total_seconds() / 60.0
    add("Signal age", age <= s.max_signal_age_min,
        f"Signal is {age:.0f} min old.",
        f"Signal is {age:.0f} min old (limit {s.max_signal_age_min}). A late entry is a different trade.")

    # 8. Already past stop or target
    sp = ctx.spot_now
    if plan.side == "CE":
        past_stop, past_target = sp <= plan.stop_index, sp >= plan.target_index
    else:
        past_stop, past_target = sp >= plan.stop_index, sp <= plan.target_index
    add("Price vs plan", not (past_stop or past_target),
        f"{plan.index} at {sp:,.2f} is between the stop and the target.",
        f"{plan.index} at {sp:,.2f} is already {'past the stop' if past_stop else 'at or past the target'}.")

    # 8b. Entry zone: don't chase a move that has mostly happened
    lim = plan.max_entry_index
    chased = sp > lim if plan.side == "CE" else sp < lim
    if not (past_stop or past_target):
        add("Entry zone", not chased,
            f"Still inside the entry zone: pay at most ₹{plan.max_entry_premium:,.2f} "
            f"({plan.index} {'up to' if plan.side == 'CE' else 'down to'} {lim:,.2f}).",
            f"{plan.index} at {sp:,.2f} is already {'above' if plan.side == 'CE' else 'below'} {lim:,.2f}: "
            f"from here the rest of the move to the target pays less than {s.min_reward_risk:g} to 1. Don't chase.")

    # 9. Expiry-day risk
    c = plan.contract
    if c.expiry == ctx.now.date():
        out.append(Check("Expiry", "BLOCK", "This contract expires today; premiums can collapse fast."))
    elif ctx.index_expiry_today:
        out.append(Check("Expiry", "WARN",
                         f"Today is a {plan.index} expiry day, so the plan uses the next expiry "
                         f"({c.expiry.day} {c.expiry.strftime('%b')})."))
    else:
        days = (c.expiry - ctx.now.date()).days
        out.append(Check("Expiry", "WARN" if days <= 1 else "PASS",
                         f"Expires in {days} day{'s' if days != 1 else ''}"
                         + (": time decay is fastest now." if days <= 1 else ".")))

    # 10. Bid/ask spread
    sp_pct = ctx.quote.spread_pct
    if sp_pct is None:
        out.append(Check("Spread", "WARN", "No live bid/ask yet; check the spread in the Angel One app."))
    else:
        add("Spread", sp_pct <= s.max_spread_pct,
            f"Bid/ask spread {sp_pct:.1f}% (limit {s.max_spread_pct:g}%).",
            f"Bid/ask spread is {sp_pct:.1f}%, wider than your {s.max_spread_pct:g}% limit.")

    # 11. Premium size
    lo, hi = s.min_premium(plan.index), s.max_premium(plan.index)
    p = plan.entry_fill
    add("Premium", lo <= p <= hi,
        f"Premium ₹{p:,.2f} is inside ₹{lo:g}-₹{hi:g}.",
        f"Premium ₹{p:,.2f} is {'too small' if p < lo else 'too large'} (allowed ₹{lo:g}-₹{hi:g}).")

    verdict = "BLOCK" if any(x.status == "BLOCK" for x in out) else "PASS"
    return RiskResult(verdict, out)
