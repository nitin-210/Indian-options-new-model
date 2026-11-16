"""Paper desk: trades you place yourself with one click. Paper only - nothing is ever sent to a broker.

You click Buy CE, Buy PE or Exit under a chart (index or stock). The desk picks everything else:
  - the contract: the tested signal's contract when you buy the side a tested rule just signalled on NIFTY/SENSEX,
    the listed trade's contract when you buy the side of a stock's running breakout on the dashboard's lists,
    otherwise the at-the-money option of the expiry the app trades (index weekly, stock monthly);
  - the price: the live ask (+1 tick) to buy and the live bid (-1 tick) to sell, so the premium, its time decay and
    its volatility are the market's own. With no live quote (e.g. a far strike in a replay) it uses the app's
    Black-Scholes estimate and says so;
  - 1 lot, from a Rs 1,00,000 paper account (cash is checked; premium paid is blocked while the trade is open);
  - the exit if you don't click Exit:
      tested signal  -> the tested rule: the signal's stop (trailing after +1R for the opening-range breakout),
                        or 15:00;
      listed stock   -> stop at the far side of the first 15 minutes, target 2R, or 15:00 (untested on stocks: paper
                        only). It does NOT exit just because price slips back inside the range: in testing (55 large
                        caps, 2022-2026) that early exit made no real difference - picks a little worse (+0.021R a trade
                        against +0.029R), movers a little better (-0.006R against -0.018R) - and you asked to keep the
                        trade to its stop or target. (The list row still disappears then: no NEW entry inside the range.)
      straddle       -> one click buys BOTH the at-the-money CE and PE (same strike, 1 lot each). No stop or target:
                        both legs close at the 15:15 square-off, or when you exit (one leg or both). With "auto
                        straddle" on, the app buys one on NIFTY 50, NIFTY BANK and SENSEX at the first price after
                        09:15 on every trading day (paper only; untested - for you to see how it behaves).
      anything else  -> stop at the far side of the first 15 minutes (kept between 0.15% and 1.5% of the price),
                        target at 2x that risk, or the 15:15 intraday square-off.
  - Indian charges on both legs (brokerage, STT, exchange, SEBI, stamp, GST), from Settings.
Each closed trade's result is split into: the underlying's move (delta), time decay (theta), volatility (IV),
spread & slippage, and charges.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import bs
from .costs import round_trip, slip
from .strategy import Quote
from .timeutil import IST, fmt_both, hm, ist

log = logging.getLogger(__name__)

CAPITAL = 100_000.0
AUTO_STRADDLE = ("NIFTY", "BANKNIFTY", "SENSEX")
MAX_LOTS = 50                 # lots per paper order (the trade bar's - / + selector)


def _lots(n) -> int:
    try:
        return max(1, min(MAX_LOTS, int(n)))
    except (TypeError, ValueError):
        return 1
AUTO_UNTIL = hm("09:30")      # the 09:15 straddle is bought at the first live price, and not after 09:30
OPEN_FROM, SQUARE_OFF = hm("09:15"), hm("15:15")
MIN_RISK, MAX_RISK = 0.0015, 0.015          # stop distance limits for your own trades, as a share of the price


class PaperError(Exception):
    pass


def _r(x, n=2):
    return None if x is None else round(float(x), n)


class PaperDesk:
    def __init__(self, engine):
        self.e = engine
        self.tokens: set[str] = set()          # stock option contracts of open paper trades (kept subscribed)
        self._marked: dict[int, datetime] = {}
        self._auto_tried: set[tuple] = set()    # (day, index) auto straddles attempted
        try:                                    # trades "closed" before they were opened (v14.22-14.23 replay bug)
            for t in engine.journal.paper_list(engine.mode, "closed"):
                if t.get("closed_at") and datetime.fromisoformat(t["closed_at"]) < datetime.fromisoformat(t["opened_at"]):
                    engine.journal.paper_delete(t["id"])
                    log.info("Removed paper trade %s: closed before it was opened (replay restarted).", t["id"])
        except Exception:  # pragma: no cover - never stop the app over a cleanup
            log.exception("paper cleanup failed")

    # ------------------------------------------------------------ helpers
    @property
    def r(self) -> float:
        return self.e.s.risk_free_rate_pct / 100.0

    def _und(self, kind: str, name: str, today: date) -> dict:
        e = self.e
        if kind == "index":
            st = e.idx.get(name)
            if st is None:
                raise PaperError("Unknown index.")
            return {"spot": st.ltp, "u": e.universes.get(name), "expiry": st.expiry, "exch": st.meta["txn"],
                    "label": st.meta["label"], "bars": st.c1.bars, "iv_hint": (e.vix.get("ltp") or 14.0) / 100.0,
                    "contracts": e.contracts}
        ms = e.movers.stocks.get(name)
        if ms is None:
            raise PaperError("Unknown stock.")
        u = e.movers.universes.get(name)
        exp = u.choose_expiry(today)[0] if u else None
        c1 = (e.stock_bars.get(name) or (None, None))[0]
        return {"spot": ms.ltp, "u": u, "expiry": exp, "exch": "nse", "label": name, "bars": c1.bars if c1 else [],
                "iv_hint": 0.30, "contracts": e.movers.option_contracts}

    def _tested(self, kind: str, name: str, side: str) -> dict | None:
        """A tested signal on this index and side whose own tracking trade is still open -> its plan.
        For a stock: its running trade on the dashboard's lists (rule "listed")."""
        e = self.e
        if kind != "index":
            row = (getattr(e, "stock_signals", None) or {}).get((name, side))
            if row:
                return {"rule": "listed", "row": row, "name": "Listed breakout",
                        "plan": {"contract": {"token": row["token"]}}}
            return None
        st = e.idx.get(name)
        if st and st.plan and not st.plan.get("error") and st.plan["side"] == side and \
                (st.trade or {}).get("status") == "open":
            return {"rule": "tested-orb", "plan": st.plan, "name": "Opening-range breakout"}
        S = getattr(e, "supertrend", None) and e.supertrend.state.get(name)
        if S and S.get("plan") and S["plan"]["side"] == side and (S.get("trade") or {}).get("status") == "open":
            return {"rule": "tested-st", "plan": S["plan"], "name": "Supertrend flip"}
        return None

    def _levels(self, und: dict, side: str, now: datetime) -> tuple[float, float]:
        """Stop and target on the underlying for your own (untested) trades."""
        spot = und["spot"]
        today = now.astimezone(IST).date()
        t930 = ist(today, hm("09:30"))
        bars = [b for b in und["bars"] if b.start.astimezone(IST).date() == today]
        first = [b for b in bars if b.start < t930]
        if first and any(b.start >= t930 for b in bars):
            hi, lo = max(b.h for b in first), min(b.l for b in first)
        elif bars:
            hi, lo = max(b.h for b in bars), min(b.l for b in bars)
        else:
            hi = lo = spot
        sd = 1 if side == "CE" else -1
        far = lo if sd == 1 else hi
        risk = min(max((spot - far) * sd, MIN_RISK * spot), MAX_RISK * spot)
        return round(spot - sd * risk, 2), round(spot + sd * 2 * risk, 2)

    def _quote_prices(self, c, und: dict, now: datetime, iv_hint: float | None = None) -> dict:
        """Buy price (ask + 1 tick), sell price (bid - 1 tick), mid and IV for a contract - live where possible."""
        q = self.e.quotes.get(c.token, Quote())
        tick = c.tick or 0.05
        sl = slip(tick, self.e.s)
        T = bs.years_to_expiry(now, c.expiry)
        spot = und["spot"]
        mid = q.mid if q.bid > 0 and q.ask > 0 else (q.ltp if q.ltp > 0 else 0.0)
        iv = bs.implied_vol(mid, spot, c.strike, T, self.r, c.opt) if mid > 0 and spot > 0 else None
        source = "live"
        if mid <= 0:
            iv = iv_hint or und["iv_hint"]
            mid = bs.price(spot, c.strike, T, self.r, iv, c.opt)
            source = "model"
        iv = iv or iv_hint or und["iv_hint"]
        pct = self.e.s.slippage_pct_no_quote / 100.0
        buy = (q.ask + sl) if q.ask > 0 else mid * (1 + pct)
        sell = (q.bid - sl) if q.bid > 0 else mid * (1 - pct)
        return {"buy": round(max(buy, tick), 2), "sell": round(max(sell, 0.05), 2), "mid": round(mid, 2), "iv": iv,
                "T": T, "source": source, "bid": q.bid, "ask": q.ask, "ltp": q.ltp}

    def _contract(self, kind: str, name: str, side: str, und: dict, tested: dict | None):
        if tested:
            c = und["contracts"].get(tested["plan"]["contract"]["token"])
            if c:
                return c
        u, exp = und["u"], und["expiry"]
        if not u or exp is None or und["spot"] <= 0:
            return None
        return u.contract(exp, u.atm(und["spot"], exp), side)

    def _open_for(self, kind: str, name: str) -> dict | None:
        """the open single CE/PE trade on this symbol (a straddle is separate: both can be open together)"""
        for t in self.e.journal.paper_list(self.e.mode, "open"):
            if t["kind"] == kind and t["name"] == name and t["rule"] != "straddle":
                return t
        return None

    def _opens_for(self, kind: str, name: str) -> list[dict]:
        return [t for t in self.e.journal.paper_list(self.e.mode, "open") if t["kind"] == kind and t["name"] == name]

    def auto_straddle_on(self) -> bool:
        return self.e.journal.paper_pref(self.e.mode, "auto_straddle", "1") == "1"

    def set_auto_straddle(self, on: bool) -> None:
        self.e.journal.paper_pref_set(self.e.mode, "auto_straddle", "1" if on else "0")

    def _straddle_legs(self, und: dict, now: datetime):
        """the CE and PE at the SAME at-the-money strike of the expiry the app trades"""
        u, exp = und["u"], und["expiry"]
        if not u or exp is None or und["spot"] <= 0:
            return None
        k = u.atm(und["spot"], exp)
        ce, pe = u.contract(exp, k, "CE"), u.contract(exp, k, "PE")
        return (ce, pe) if ce and pe else None

    # ------------------------------------------------------------ account
    def account(self, now: datetime) -> dict:
        e = self.e
        capital, since = e.journal.paper_capital(e.mode, CAPITAL)
        rows = e.journal.paper_list(e.mode)
        closed = [t for t in rows if t["status"] == "closed"]
        opened = [t for t in rows if t["status"] == "open"]
        realized = sum(t["pnl"] or 0 for t in closed)
        blocked = sum(t["entry_premium"] * t["qty"] for t in opened)
        open_pnl = 0.0
        for t in opened:
            m = self._mark_trade(t, now)
            if m:
                open_pnl += m["pnl"]
        today = now.astimezone(IST).date().isoformat()
        wins = [t["pnl"] for t in closed if (t["pnl"] or 0) > 0]
        losses = [t["pnl"] for t in closed if (t["pnl"] or 0) <= 0]
        splits = [json.loads(t["split_json"]) for t in closed if t.get("split_json")]
        return {"capital": capital, "since": since, "balance": round(capital + realized, 2),
                "realized": round(realized, 2), "open_pnl": round(open_pnl, 2),
                "equity": round(capital + realized + open_pnl, 2), "cash": round(capital + realized - blocked, 2),
                "blocked": round(blocked, 2), "trades": len(closed), "open": len(opened),
                "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / len(closed), 3) if closed else None,
                "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                "today": round(sum(t["pnl"] or 0 for t in closed if t["day"] == today) + open_pnl, 2),
                "charges": round(sum(t["costs"] or 0 for t in closed), 2),
                "theta": round(sum(s.get("theta", 0) for s in splits), 2),
                "delta": round(sum(s.get("delta", 0) for s in splits), 2),
                "vega": round(sum(s.get("vega", 0) for s in splits), 2), "mode": e.mode}

    # ------------------------------------------------------------ ticket (what one click would do)
    def ticket(self, kind: str, name: str, now: datetime, lots: int = 1) -> dict:
        e = self.e
        lots = _lots(lots)
        today = now.astimezone(IST).date()
        und = self._und(kind, name, today)
        acct = self.account(now)
        open_t = self._open_for(kind, name)
        feed = e.feed_status(now)
        t = now.astimezone(IST).time()
        closed_why = None
        if not e.cal.is_trading_day(today):
            closed_why = "Market holiday."
        elif t < OPEN_FROM or t >= SQUARE_OFF:
            closed_why = "Paper buys are open 09:15-15:15 IST."
        elif not feed["live"]:
            closed_why = "Prices are not live right now."
        elif und["spot"] <= 0:
            closed_why = "No price for this symbol yet."
        sides = {}
        for side in ("CE", "PE"):
            tested = self._tested(kind, name, side)
            c = self._contract(kind, name, side, und, tested)
            if c is None:
                sides[side] = {"can_buy": False, "why": "No option contract found for it."}
                continue
            pr = self._quote_prices(c, und, now)
            if tested and tested["rule"] == "listed":
                row = tested["row"]
                stop, target = row["stop"], row["target"]
                rule = f"Listed breakout ({row['time']}): stop {stop:,.2f}, target {target:,.2f} (2R), or 15:00"
            elif tested:
                p = tested["plan"]
                stop, target = p["stop_index"], None
                exit_t = datetime.fromisoformat(p["exit_time"]).astimezone(IST).strftime("%H:%M")
                rule = (f"Tested {tested['name']}: stop {p['stop_index']:,.2f}"
                        + (" (trails after +1R)" if p.get("exit_mode") == "trail" else "") + f", exit {exit_t}")
            else:
                stop, target = self._levels(und, side, now)
                rule = f"Your call: stop {stop:,.2f}, target {target:,.2f} (2R), square-off 15:15"
            T, iv = pr["T"], pr["iv"]
            model_now = bs.price(und["spot"], c.strike, T, self.r, iv, c.opt) if und["spot"] > 0 else pr["mid"]
            at = lambda lvl: max(c.tick or 0.05, pr["mid"] + bs.price(lvl, c.strike, T, self.r, iv, c.opt) - model_now)
            qty = c.lot * lots
            cost = pr["buy"] * qty
            est_stop = at(stop) if stop else None
            loss = None
            if est_stop is not None:
                ch = round_trip(pr["buy"], est_stop, qty, und["exch"], e.s)
                loss = qty * (pr["buy"] - est_stop) + ch.total
            why = closed_why or ("You already have an open paper trade on this - exit it first." if open_t else None) \
                or (f"Not enough paper cash: needs ₹{cost:,.0f}, free ₹{acct['cash']:,.0f}." if cost > acct["cash"] else None)
            sides[side] = {"can_buy": why is None, "why": why, "token": c.token, "contract": _label(c, name),
                           "strike": c.strike, "expiry": c.expiry.isoformat(), "lot": c.lot, "qty": qty,
                           "price": pr["buy"], "price_source": pr["source"], "bid": _r(pr["bid"]), "ask": _r(pr["ask"]),
                           "cost": round(cost, 2), "stop": _r(stop), "target": _r(target),
                           "est_stop_premium": _r(est_stop), "est_target_premium": _r(at(target)) if target else None,
                           "max_loss": _r(loss, 0), "rule": rule, "tested": bool(tested),
                           "signal": (tested or {}).get("rule"),
                           "iv": _r(iv * 100, 1), "delta": _r(bs.delta(und["spot"], c.strike, T, self.r, iv, c.opt), 2)
                           if und["spot"] > 0 else None,
                           "theta_day": _r(bs.theta_per_day(und["spot"], c.strike, T, self.r, iv, c.opt) * qty, 0)
                           if und["spot"] > 0 else None}
        straddle = {"can_buy": False, "why": "No option contracts found for it."}
        legs = self._straddle_legs(und, now)
        if legs:
            ce, pe = legs
            pc, pp = self._quote_prices(ce, und, now), self._quote_prices(pe, und, now)
            cost = (pc["buy"] * ce.lot + pp["buy"] * pe.lot) * lots
            per = pc["buy"] + pp["buy"]
            has_str = any(t["rule"] == "straddle" for t in self._opens_for(kind, name))
            why = closed_why or ("You already have an open straddle on this - exit it first." if has_str else None) \
                or (f"Not enough paper cash: needs ₹{cost:,.0f}, free ₹{acct['cash']:,.0f}." if cost > acct["cash"] else None)
            straddle = {"can_buy": why is None, "why": why, "strike": ce.strike, "expiry": ce.expiry.isoformat(),
                        "contract": f"{name} {ce.strike:,.0f} CE + PE {ce.expiry:%d %b}", "qty": ce.lot * lots,
                        "price_ce": pc["buy"], "price_pe": pp["buy"], "per_unit": round(per, 2), "cost": round(cost, 2),
                        "source": "model" if "model" in (pc["source"], pp["source"]) else "live",
                        "breakeven_up": round(ce.strike + per, 2), "breakeven_dn": round(ce.strike - per, 2),
                        "rule": "Straddle: both at-the-money options, no stop; closes at 15:15 or when you exit"}
        sys_side = next((s for s in ("CE", "PE") if sides[s].get("tested")), None)
        sig = self._tested(kind, name, sys_side) if sys_side else None
        if sig and sig["rule"] == "listed":
            row = sig["row"]
            text = (f"System: BUY {sys_side} - broke {'up' if sys_side == 'CE' else 'down'} at {row['time']} "
                    f"(on the {'picks' if row['list'] == 'picks' else 'breakouts'} list). Untested on stocks: paper only.")
        elif sig:
            text = f"System: BUY {sys_side} ({sig['name']}, tested)"
        elif kind == "index":
            text = "No signal right now: buying is your own call."
        else:
            text = "No signal on this stock right now: buying is your own call."
        return {"kind": kind, "name": name, "label": und["label"], "spot": _r(und["spot"]), "lots": lots,
                "max_lots": MAX_LOTS, "system_side": sys_side,
                "system_text": text, "sides": sides, "straddle": straddle,
                "open": self._trade_json(open_t, now) if open_t else None,
                "open_list": [self._trade_json(t, now) for t in self._opens_for(kind, name)],
                "straddle_open": [self._trade_json(t, now) for t in self._opens_for(kind, name) if t["rule"] == "straddle"],
                "account": acct, "closed_why": closed_why}

    # ------------------------------------------------------------ open / exit
    def open(self, kind: str, name: str, side: str, now: datetime, lots: int = 1) -> dict:
        if side not in ("CE", "PE") or kind not in ("index", "stock"):
            raise PaperError("Choose Buy CE or Buy PE.")
        lots = _lots(lots)
        tk = self.ticket(kind, name, now, lots)
        sd = tk["sides"][side]
        if not sd.get("can_buy"):
            raise PaperError(sd.get("why") or "Can't buy right now.")
        e = self.e
        today = now.astimezone(IST).date()
        und = self._und(kind, name, today)
        tested = self._tested(kind, name, side)
        c = und["contracts"][sd["token"]]
        pr = self._quote_prices(c, und, now)
        if tested and tested["rule"] == "listed":
            row = tested["row"]
            rule_json = {"listed": {k: row[k] for k in ("time", "list", "entry", "stop", "target", "range_high",
                                                        "range_low")}}
        else:
            rule_json = {"plan": tested["plan"]} if tested else {"square_off": "15:15"}
        greeks = {"iv": pr["iv"], "T": pr["T"], "spot": und["spot"], "mid": pr["mid"], "source": pr["source"],
                  "delta": bs.delta(und["spot"], c.strike, pr["T"], self.r, pr["iv"], c.opt),
                  "theta_day": bs.theta_per_day(und["spot"], c.strike, pr["T"], self.r, pr["iv"], c.opt),
                  "vega": bs.vega(und["spot"], c.strike, pr["T"], self.r, pr["iv"])}
        tid = e.journal.paper_add({
            "mode": e.mode, "day": today.isoformat(), "kind": kind, "name": name, "side": side, "symbol": c.symbol,
            "token": c.token, "exch": und["exch"], "expiry": c.expiry.isoformat(), "strike": c.strike, "lot": c.lot,
            "lots": lots, "qty": c.lot * lots, "tick": c.tick or 0.05, "opened_at": now.isoformat(), "entry_premium": pr["buy"],
            "entry_spot": und["spot"], "stop_spot": sd["stop"], "target_spot": sd["target"],
            "rule": tested["rule"] if tested else "manual", "rule_json": json.dumps(rule_json), "status": "open",
            "greeks_json": json.dumps(greeks)})
        self._subscribe(kind, c.token)
        e.log_event("paper", f"Paper BUY {_label(c, name)} x{c.lot * lots} ({lots} lot{'s' if lots > 1 else ''}) at ₹{pr['buy']:,.2f} ({pr['source']} price) · "
                             f"{sd['rule']}.")
        return self._trade_json(e.journal.paper_get(tid), now)

    def open_straddle(self, kind: str, name: str, now: datetime, auto: bool = False, lots: int = 1) -> list[dict]:
        if kind not in ("index", "stock"):
            raise PaperError("Unknown symbol.")
        lots = _lots(lots)
        tk = self.ticket(kind, name, now, lots)
        sq = tk["straddle"]
        if not sq.get("can_buy"):
            raise PaperError(sq.get("why") or "Can't buy right now.")
        e = self.e
        today = now.astimezone(IST).date()
        und = self._und(kind, name, today)
        legs = self._straddle_legs(und, now)
        if not legs:
            raise PaperError("No option contracts found for it.")
        group = f"{name}-{now.astimezone(IST):%Y%m%d-%H%M%S}"
        out = []
        for c in legs:
            pr = self._quote_prices(c, und, now)
            greeks = {"iv": pr["iv"], "T": pr["T"], "spot": und["spot"], "mid": pr["mid"], "source": pr["source"],
                      "delta": bs.delta(und["spot"], c.strike, pr["T"], self.r, pr["iv"], c.opt),
                      "theta_day": bs.theta_per_day(und["spot"], c.strike, pr["T"], self.r, pr["iv"], c.opt),
                      "vega": bs.vega(und["spot"], c.strike, pr["T"], self.r, pr["iv"])}
            tid = e.journal.paper_add({
                "mode": e.mode, "day": today.isoformat(), "kind": kind, "name": name, "side": c.opt,
                "symbol": c.symbol, "token": c.token, "exch": und["exch"], "expiry": c.expiry.isoformat(),
                "strike": c.strike, "lot": c.lot, "lots": lots, "qty": c.lot * lots, "tick": c.tick or 0.05,
                "opened_at": now.isoformat(), "entry_premium": pr["buy"], "entry_spot": und["spot"],
                "stop_spot": None, "target_spot": None, "rule": "straddle",
                "rule_json": json.dumps({"straddle": group, "auto": auto, "square_off": "15:15"}), "status": "open",
                "greeks_json": json.dumps(greeks)})
            self._subscribe(kind, c.token)
            out.append(self._trade_json(e.journal.paper_get(tid), now))
        e.log_event("paper", f"Paper BUY straddle{' (auto 09:15)' if auto else ''} {sq['contract']} x{legs[0].lot * lots}: "
                             f"CE ₹{out[0]['entry_premium']:,.2f} + PE ₹{out[1]['entry_premium']:,.2f} = "
                             f"₹{sq['cost']:,.0f} · no stop, closes 15:15 or when you exit.")
        return out

    def exit_group(self, group: str, now: datetime, reason: str = "you exited") -> list[dict]:
        rows = [t for t in self.e.journal.paper_list(self.e.mode, "open")
                if json.loads(t.get("rule_json") or "{}").get("straddle") == group]
        if not rows:
            raise PaperError("No such open straddle.")
        return [self.exit(t["id"], now, reason) for t in rows]

    def _auto_straddles(self, now: datetime, feed: dict) -> None:
        """buy the 09:15 straddle on NIFTY 50, NIFTY BANK and SENSEX at the first live price of the day"""
        e = self.e
        today = now.astimezone(IST).date()
        t = now.astimezone(IST).time()
        if not (OPEN_FROM <= t < AUTO_UNTIL) or not feed["live"] or not e.cal.is_trading_day(today):
            return
        if not self.auto_straddle_on():
            return
        done = {(r["name"]) for r in e.journal.paper_list(e.mode) if r["day"] == today.isoformat()
                and r["rule"] == "straddle" and json.loads(r.get("rule_json") or "{}").get("auto")}
        for name in AUTO_STRADDLE:
            if name in done or (today, name) in self._auto_tried or name not in e.idx or e.idx[name].ltp <= 0:
                continue
            self._auto_tried.add((today, name))
            try:
                self.open_straddle("index", name, now, auto=True)
            except PaperError as ex:
                e.log_event("paper", f"Auto 09:15 straddle on {e.idx[name].meta['label']} skipped: {ex}")

    def exit(self, trade_id: int, now: datetime, reason: str = "you exited") -> dict:
        e = self.e
        t = e.journal.paper_get(trade_id)
        if not t or t["mode"] != e.mode:
            raise PaperError("No such paper trade.")
        if t["status"] != "open":
            return self._trade_json(t, now)
        m = self._mark_trade(t, now)
        if m is None:
            raise PaperError("No price to sell at right now.")
        return self._close(t, m, now, reason)

    def _close(self, t: dict, m: dict, now: datetime, reason: str) -> dict:
        e = self.e
        closed = e.journal.paper_close(t["id"], {
            "closed_at": now.isoformat(), "exit_premium": m["price"], "exit_spot": m["spot"], "exit_reason": reason,
            "costs": round(m["costs"], 2), "pnl": round(m["pnl"], 2), "split_json": json.dumps(m["split"])})
        self.tokens.discard(t["token"])
        e.log_event("paper", f"Paper SELL {t['symbol']} x{t['qty']} at ₹{m['price']:,.2f} ({reason}) · "
                             f"P&L after charges ₹{m['pnl']:,.0f}.")
        e.hub.publish("alert", {"index": t["name"], "headline": f"Paper trade closed: {reason}",
                                "text": f"{t['symbol']}: ₹{m['pnl']:,.0f} after charges", "verdict": "EXIT"})
        return self._trade_json(closed, now)

    def _subscribe(self, kind: str, token: str) -> None:
        e = self.e
        if kind == "index":
            e.selected_tokens.add(token)
        else:
            self.tokens.add(token)
        e._recentre(force=True)

    # ------------------------------------------------------------ marking and the P&L split
    def _mark_trade(self, t: dict, now: datetime) -> dict | None:
        e = self.e
        today = now.astimezone(IST).date()
        try:
            und = self._und(t["kind"], t["name"], today)
        except PaperError:
            return None
        c = und["contracts"].get(t["token"])
        g = json.loads(t["greeks_json"] or "{}")
        if c is None or und["spot"] <= 0:
            if t.get("exit_premium"):                    # last saved mark
                px = t["exit_premium"]
                ch = round_trip(t["entry_premium"], px, t["qty"], t["exch"], e.s)
                return {"price": px, "spot": t.get("exit_spot") or t["entry_spot"], "costs": ch.total,
                        "pnl": t["qty"] * (px - t["entry_premium"]) - ch.total, "split": {}, "source": "saved"}
            return None
        pr = self._quote_prices(c, und, now, g.get("iv"))
        px = pr["sell"]
        ch = round_trip(t["entry_premium"], px, t["qty"], t["exch"], e.s)
        pnl = t["qty"] * (px - t["entry_premium"]) - ch.total
        # split: the underlying's move, time decay and volatility, priced with the entry IV, then the change in IV
        K, opt, r = t["strike"], t["side"], self.r
        S0, S1 = g.get("spot") or t["entry_spot"], und["spot"]
        T0, T1 = g.get("T") or pr["T"], pr["T"]
        iv0, iv1 = g.get("iv") or pr["iv"], pr["iv"]
        p = lambda S, T, v: bs.price(S, K, T, r, v, opt)
        move = p(S1, T0, iv0) - p(S0, T0, iv0)
        decay = p(S1, T1, iv0) - p(S1, T0, iv0)
        vol = p(S1, T1, iv1) - p(S1, T1, iv0)
        rest = (px - t["entry_premium"]) - (move + decay + vol)
        q = t["qty"]
        split = {"delta": round(move * q, 2), "theta": round(decay * q, 2), "vega": round(vol * q, 2),
                 "spread": round(rest * q, 2), "charges": round(-ch.total, 2), "iv_exit": round(iv1 * 100, 1),
                 "iv_entry": round(iv0 * 100, 1)}
        return {"price": px, "spot": S1, "costs": ch.total, "pnl": pnl, "split": split, "source": pr["source"],
                "mid": pr["mid"], "bid": pr["bid"], "ask": pr["ask"]}

    def _trade_json(self, t: dict | None, now: datetime) -> dict | None:
        if not t:
            return None
        d = {k: t[k] for k in ("id", "kind", "name", "side", "symbol", "expiry", "strike", "lot", "qty", "entry_premium",
                               "entry_spot", "stop_spot", "target_spot", "rule", "status", "exit_premium", "exit_spot",
                               "exit_reason", "costs", "pnl", "day")}
        d["contract"] = f"{t['name']} {t['strike']:,.0f} {t['side']} {date.fromisoformat(t['expiry']):%d %b}"
        d["opened"] = datetime.fromisoformat(t["opened_at"]).astimezone(IST).strftime("%H:%M")
        d["closed"] = datetime.fromisoformat(t["closed_at"]).astimezone(IST).strftime("%H:%M") if t.get("closed_at") else None
        d["greeks"] = json.loads(t["greeks_json"] or "{}")
        d["split"] = json.loads(t["split_json"]) if t.get("split_json") else None
        d["invested"] = round(t["entry_premium"] * t["qty"], 2)
        rj = json.loads(t.get("rule_json") or "{}")
        d["rule_text"] = {"tested-orb": "Tested opening-range breakout: stop (trails after +1R) or 15:00",
                          "tested-st": "Tested Supertrend flip: stop or 15:00",
                          "listed": "Listed stock breakout: stop, 2R target or 15:00",
                          "straddle": "Straddle (CE + PE): no stop, 15:15 square-off or when you exit",
                          "manual": "Your call: stop, 2R target or the 15:15 square-off"}.get(t["rule"], t["rule"])
        d["group"] = rj.get("straddle")
        d["auto"] = bool(rj.get("auto"))
        if t["status"] == "open":
            m = self._mark_trade(t, now)
            if m:
                d.update({"mark": m["price"], "mark_source": m["source"], "live_pnl": round(m["pnl"], 2),
                          "live_split": m["split"], "spot_now": m["spot"]})
            if t["rule"] == "tested-orb" and rj.get("plan"):
                d["stop_now"] = self._stop_now(t, rj)
        return d

    def _stop_now(self, t: dict, rj: dict) -> float:
        from .engine import trail_stop
        p = rj["plan"]
        st = self.e.idx.get(t["name"])
        if p.get("exit_mode") == "trail" and st is not None:
            return round(trail_stop(p, st.c5.bars)[0], 2)
        return p["stop_index"]

    def trades(self, now: datetime) -> dict:
        rows = self.e.journal.paper_list(self.e.mode)
        return {"account": self.account(now), "auto_straddle": self.auto_straddle_on(),
                "open": [self._trade_json(t, now) for t in rows if t["status"] == "open"],
                "closed": [self._trade_json(t, now) for t in reversed(rows) if t["status"] == "closed"]}

    # ------------------------------------------------------------ automatic exits (Engine.evaluate)
    def step(self, now: datetime, feed: dict) -> None:
        e = self.e
        today = now.astimezone(IST).date()
        try:
            self._auto_straddles(now, feed)
        except Exception:
            log.exception("auto straddle failed")
        for t in e.journal.paper_list(e.mode, "open"):
            try:
                self._step_trade(t, now, today, feed)
            except Exception:
                log.exception("paper step failed for trade %s", t.get("id"))

    def _step_trade(self, t: dict, now: datetime, today: date, feed: dict) -> None:
        e = self.e
        if e.mode in ("replay", "demo") and datetime.fromisoformat(t["opened_at"]) > now + timedelta(minutes=1):
            # opened at a later time of an earlier run of this replay: it never happened in this run
            e.journal.paper_delete(t["id"])
            self.tokens.discard(t["token"])
            e.log_event("paper", f"Removed paper trade {t['symbol']} from an earlier run of this replay (opened "
                                 f"{datetime.fromisoformat(t['opened_at']).astimezone(IST):%H:%M}, after this replay's time).")
            return
        if t["kind"] == "stock" and t["token"] not in self.tokens:
            self._subscribe("stock", t["token"])
        elif t["kind"] == "index" and t["token"] not in e.selected_tokens:
            self._subscribe("index", t["token"])
        if date.fromisoformat(t["day"]) < today:          # the app was off at the square-off
            px = t.get("exit_premium") or t["entry_premium"]
            ch = round_trip(t["entry_premium"], px, t["qty"], t["exch"], e.s)
            self._close(t, {"price": px, "spot": t.get("exit_spot") or t["entry_spot"], "costs": ch.total,
                            "pnl": t["qty"] * (px - t["entry_premium"]) - ch.total, "split": {}},
                        now, "closed at the last price seen (app was off at 15:15)")
            return
        m = self._mark_trade(t, now)
        if m is None:
            return
        last = self._marked.get(t["id"])
        if last is None or (now - last).total_seconds() >= 60:          # remember the last mark (app may stop)
            self._marked[t["id"]] = now
            with e.journal._lock:
                e.journal._db.execute("UPDATE paper_trades SET exit_premium=?, exit_spot=? WHERE id=? AND status='open'",
                                      (m["price"], m["spot"], t["id"]))
                e.journal._db.commit()
        rj = json.loads(t.get("rule_json") or "{}")
        sd = 1 if t["side"] == "CE" else -1
        spot = m["spot"]
        if t["rule"] in ("tested-orb", "tested-st") and rj.get("plan"):
            p = rj["plan"]
            exit_t = datetime.fromisoformat(p["exit_time"])
            stop = self._stop_now(t, rj) if t["rule"] == "tested-orb" else p["stop_index"]
            target = None
            trailing = t["rule"] == "tested-orb" and stop != p["stop_index"]
        elif t["rule"] == "listed":
            exit_t = ist(today, hm("15:00"))
            stop, target, trailing = t["stop_spot"], t["target_spot"], False
        else:
            exit_t = ist(today, SQUARE_OFF)
            stop, target, trailing = t["stop_spot"], t["target_spot"], False
        reason = None
        if now >= exit_t:
            reason = "time exit " + exit_t.astimezone(IST).strftime("%H:%M")
        elif feed["live"] and stop is not None and (spot - stop) * sd <= 0:
            reason = "trailing stop" if trailing else "stop hit"
        elif feed["live"] and target is not None and (spot - target) * sd >= 0:
            reason = "target hit"
        if reason:
            self._close(t, m, now, reason)



def _label(c, name: str) -> str:
    return f"{name} {c.strike:,.0f} {c.opt} {c.expiry:%d %b}"
