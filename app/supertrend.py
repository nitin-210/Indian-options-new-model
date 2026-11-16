"""Second tested strategy: Supertrend (10, 3) on 15-minute candles, NIFTY and SENSEX (signals only, paper-tracked).

Rule, exactly as tested (research/lab.py, "Supertrend 15m flip", exit "hold"):
  - 15-minute candles built from the 5-minute ones (09:15-09:30, 09:30-09:45, ...); Supertrend with a 10-candle ATR
    (Wilder smoothing) and a 3x band, warmed up on the previous sessions' 15-minute candles.
  - Signal: the FIRST time today the Supertrend direction flips at a 15-minute close from 09:45 to 14:00
    (up = BUY CE, down = BUY PE), at that candle's close. One trade per index per day.
  - Stop: the Supertrend line at the signal (on the index). Exit: the stop, or 15:00.
Tested Jan 2015 - Nov 2026 (NIFTY, SENSEX 2018+), 1 lot ATM, after charges: about 48% of trades won, average win
about 1.6x the average loss, NIFTY profitable in 12 of 12 years and SENSEX in 8 of 9 (with the app's option-price
model; a pessimistic pricing stress test was negative, so it is paper-tracked here first).

Uses the app's own option plan, risk check and paper journal (strategy "supertrend").
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta

from . import odds
from . import risk as riskmod
from .costs import round_trip, slip
from .strategy import OpeningRange, Quote, Signal, make_plan
from .timeutil import IST, fmt_both, hm, ist

log = logging.getLogger(__name__)

KEYS = ("NIFTY", "SENSEX")
N, MULT = 10, 3.0
FIRST_END, LAST_END = hm("09:45"), hm("14:00")
WARMUP_DAYS = 90
NAME = "Supertrend flip"


def _ema(x: list[float], n: int) -> list[float]:
    a = 2 / (n + 1)
    out = [x[0]]
    for v in x[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def supertrend(h: list[float], l: list[float], c: list[float], n: int = N, mult: float = MULT):
    """(line, direction) per candle; direction 1 = up (line below price), -1 = down. Same as the research version."""
    m = len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, m)]
    atr = _ema(tr, 2 * n - 1)
    ub = [(h[i] + l[i]) / 2 + mult * atr[i] for i in range(m)]
    lb = [(h[i] + l[i]) / 2 - mult * atr[i] for i in range(m)]
    fub, flb = ub[:], lb[:]
    line, dirn = [0.0] * m, [1] * m
    for i in range(1, m):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if dirn[i - 1] == 1:
            dirn[i] = -1 if c[i] < flb[i] else 1
        else:
            dirn[i] = 1 if c[i] > fub[i] else -1
        line[i] = flb[i] if dirn[i] == 1 else fub[i]
    return line, dirn


class SupertrendStrategy:
    def __init__(self, engine):
        self.e = engine
        self.day: date | None = None
        self._hist: dict[str, tuple] = {}
        self._calc: dict[str, tuple] = {}
        self.state: dict[str, dict] = {k: self._blank() for k in KEYS}

    @staticmethod
    def _blank() -> dict:
        return {"phase": "waiting", "signal": None, "plan": None, "plan_obj": None, "risk": None, "trade_id": None,
                "trade": None, "dir": None, "line": None, "seen_at": None, "missed": None, "loaded": False}

    def reset(self) -> None:
        self.day = None
        self._calc = {}
        self.state = {k: self._blank() for k in KEYS}

    # ------------------------------------------------------------ candles
    def _history15(self, key: str, today: date):
        bars = self.e.hist_bars.get(key) or []
        tag = (len(bars), today)
        if key not in self._hist or self._hist[key][0] != tag:
            sess = odds.full_sessions([b for b in bars if b.start.astimezone(IST).date() < today])
            H, L, C = [], [], []
            for d in sorted(sess)[-WARMUP_DAYS:]:
                v = sess[d]
                for k in range(0, len(v) - 2, 3):
                    g = v[k:k + 3]
                    H.append(max(b.h for b in g)); L.append(min(b.l for b in g)); C.append(g[-1].c)
            self._hist[key] = (tag, H, L, C)
        return self._hist[key][1:]

    @staticmethod
    def _today15(st, today: date) -> list[tuple]:
        """Today's completed 15-minute candles from 09:15, in order: (end time, high, low, close)."""
        by: dict[int, list] = {}
        for b in st.c5.bars:
            t = b.start.astimezone(IST)
            if t.date() != today or not b.closed:
                continue
            by.setdefault(((t.hour * 60 + t.minute) - 555) // 15, []).append(b)
        out, k, t0 = [], 0, ist(today, hm("09:15"))
        while k in by and len(by[k]) == 3:
            g = sorted(by[k], key=lambda b: b.start)
            out.append((t0 + timedelta(minutes=15 * (k + 1)), max(b.h for b in g), min(b.l for b in g), g[-1].c))
            k += 1
        return out

    def _calc_key(self, key: str, today: date):
        st = self.e.idx[key]
        t15 = self._today15(st, today)
        tag = (len(t15), today, len(self.e.hist_bars.get(key) or []))
        if self._calc.get(key, (None,))[0] == tag:
            return self._calc[key][1:]
        H, L, C = self._history15(key, today)
        if len(H) < 3 * N or not t15:
            self._calc[key] = (tag, t15, None, None)
            return t15, None, None
        line, dirn = supertrend(H + [x[1] for x in t15], L + [x[2] for x in t15], C + [x[3] for x in t15])
        n = len(t15)
        self._calc[key] = (tag, t15, line[-n:], dirn[-n:])
        return t15, line[-n:], dirn[-n:]

    # ------------------------------------------------------------ main step (called by Engine.evaluate)
    def step(self, now: datetime, feed: dict) -> None:
        today = now.astimezone(IST).date()
        if self.day != today:
            self.reset()
            self.day = today
        if not self.e.cal.is_trading_day(today):
            return
        for key in KEYS:
            if key not in self.e.idx:
                continue
            try:
                self._step_key(key, now, today, feed)
            except Exception:                         # never let the second strategy break the engine
                log.exception("supertrend step failed for %s", key)

    def _step_key(self, key: str, now: datetime, today: date, feed: dict) -> None:
        S = self.state[key]
        e = self.e
        if not S["loaded"]:                           # app restarted mid-day: pick today's trade back up
            S["loaded"] = True
            tr = e.journal.find_open(today, key, e.mode, "supertrend")
            if tr:
                S["trade_id"], S["trade"] = tr["id"], tr
                S["plan"] = json.loads(tr["plan_json"]) if tr.get("plan_json") else None
                p = S["plan"] or {}
                S["signal"] = {"time": datetime.fromisoformat(p["signal_time"]).astimezone(IST).strftime("%H:%M"),
                               "side": p["side"], "close": p["entry_index"], "stop": p["stop_index"]} if p else None
        t15, line, dirn = self._calc_key(key, today)
        if dirn:
            S["dir"], S["line"] = dirn[-1], round(line[-1], 2)
        if S["signal"] is None and dirn:
            for k in range(1, len(t15)):
                end = t15[k][0]
                if end.time() < FIRST_END:
                    continue
                if end.time() > LAST_END:
                    break
                if dirn[k] != dirn[k - 1]:
                    side = "CE" if dirn[k] == 1 else "PE"
                    S["signal"] = {"time": end.strftime("%H:%M"), "end": end, "side": side, "close": t15[k][3],
                                   "stop": line[k]}
                    S["seen_at"] = now
                    late = (now - end).total_seconds() > e.s.max_signal_age_min * 60
                    if late and S["trade_id"] is None:
                        S["missed"] = True
                        e.log_event("signal", f"{e.idx[key].meta['label']} {NAME}: the flip at {fmt_both(end)} "
                                              "happened while the app was off. Shown as missed: no paper trade.")
                    else:
                        e.log_event("signal", f"{e.idx[key].meta['label']} {NAME}: the 15-minute candle ending "
                                              f"{fmt_both(end)} flipped the Supertrend {'up' if side == 'CE' else 'down'} "
                                              f"at {t15[k][3]:,.2f} (stop {line[k]:,.2f}) -> BUY {side}")
                    break
        if S["signal"] is None:
            S["phase"] = "waiting" if now.time() < LAST_END else "none"
            return
        if S["missed"]:
            S["phase"] = "missed"
            return
        if S["trade_id"] is None:
            self._try_plan(key, S, now, feed)
        if S["trade_id"]:
            self._monitor(key, S, now, feed)
        S["phase"] = "signal" if not S["trade"] or S["trade"].get("status") == "open" else "closed"

    # ------------------------------------------------------------ plan + paper trade
    def _try_plan(self, key: str, S: dict, now: datetime, feed: dict) -> None:
        e = self.e
        st = e.idx[key]
        u = e.universes.get(key)
        sig = S["signal"]
        if not u or st.expiry is None:
            return
        c = u.contract(st.expiry, u.pick_strike(sig["close"], st.expiry, sig["side"], e.s.strike_mode), sig["side"])
        if c is None:
            return
        if c.token not in e.selected_tokens:
            e.selected_tokens.add(c.token)
            e._recentre(force=True)
        q = e.quotes.get(c.token, Quote())
        if q.ltp <= 0 and (now - (S["seen_at"] or now)).total_seconds() < 10:
            return
        end = sig["end"]
        signal = Signal(key, sig["side"], end - timedelta(minutes=15), end, sig["close"],
                        max(sig["close"], sig["stop"]), min(sig["close"], sig["stop"]))
        orng = OpeningRange(signal.or_high, signal.or_low, 3, 3, True)          # far side = the Supertrend line
        s2 = replace(e.s, stop_rule="far", exit_mode="hold")
        plan = make_plan(signal, orng, c, q, st.ltp or sig["close"], now, s2, st.meta["txn"], st.expiry_flags)
        from .engine import plan_text
        pj = plan.to_json()
        pj.update(plan_text(pj))
        pj["strategy"] = "supertrend"
        pj["headline"] = f"{NAME}: " + pj["headline"]
        S["plan_obj"], S["plan"] = plan, pj
        today = e.journal.today(now.date(), e.mode)
        ctx = riskmod.RiskContext(now=now, feed_live=feed["live"], feed_reason=feed["reason"] or feed["state"],
                                  market_open=feed["phase"] == "open", trades_today=today["trades"],
                                  realized_today=today["realized"], open_risk=today["open_risk"],
                                  spot_now=st.ltp, quote=q, index_expiry_today=st.expiry_today, demo=e.mode != "live")
        S["risk"] = riskmod.check(plan, ctx, e.s).to_json()
        S["trade_id"] = e.journal.open_trade(day=now.date(), plan=pj, verdict=S["risk"]["verdict"],
                                             reasons=S["risk"]["blocks"], mode=e.mode, strategy="supertrend")
        S["trade"] = e.journal.get(S["trade_id"])
        text = (f"{pj['headline']} · Stop: {pj['stop_text']} · Exit by 15:00 · {pj['size_text']} · "
                f"Risk check {S['risk']['verdict']}")
        e.hub.publish("alert", {"index": key, "headline": pj["headline"], "verdict": S["risk"]["verdict"], "text": text})
        e.log_event("plan", text, "info" if S["risk"]["verdict"] == "PASS" else "warn")

    def _monitor(self, key: str, S: dict, now: datetime, feed: dict) -> None:
        e = self.e
        tr = e.journal.get(S["trade_id"])
        S["trade"] = tr
        if not tr or tr["status"] != "open" or not S["plan"]:
            return
        p = S["plan"]
        st = e.idx[key]
        q = e.quotes.get(p["contract"]["token"], Quote())
        exit_t = datetime.fromisoformat(p["exit_time"])
        reason = None
        if now >= exit_t:
            reason = "time"
        elif feed["live"] and st.ltp > 0:
            if (st.ltp <= p["stop_index"]) if p["side"] == "CE" else (st.ltp >= p["stop_index"]):
                reason = "stop"
        if reason is None:
            if q.ltp > 0:
                px = q.bid if q.bid > 0 else q.ltp
                c = round_trip(tr["entry_fill"], px, tr["qty"], st.meta["txn"], e.s)
                S["trade"] = dict(tr, live_pnl=round(tr["qty"] * (px - tr["entry_fill"]) - c.total, 2))
            return
        if reason == "time" and not feed["live"] and q.ltp <= 0:
            return
        tick = p["contract"].get("tick") or 0.05
        fill = (q.bid - slip(tick, e.s)) if q.bid > 0 else q.ltp * (1 - e.s.slippage_pct_no_quote / 100)
        fill = max(fill, 0.05)
        c = round_trip(tr["entry_fill"], fill, tr["qty"], st.meta["txn"], e.s)
        closed = e.journal.close_trade(S["trade_id"], exit_time=now, exit_index=st.ltp, exit_fill=round(fill, 2),
                                       reason=reason, costs=c.total)
        S["trade"] = closed
        what = "time exit at 15:00" if reason == "time" else "stop hit"
        e.log_event("exit", f"{st.meta['label']} {NAME} {p['contract']['label']}: {what}. Exit ≈ ₹{fill:,.2f}, "
                            f"P&L after costs ₹{closed.get('pnl', 0):,.0f}.")
        e.hub.publish("alert", {"index": key, "headline": f"{st.meta['label']} {NAME}: {what}",
                                "text": f"P&L after costs ₹{closed.get('pnl', 0):,.0f}", "verdict": "EXIT"})

    # ------------------------------------------------------------ for the dashboard
    def snapshot(self) -> dict:
        out = {}
        for key, S in self.state.items():
            sig = S["signal"]
            out[key] = {"phase": S["phase"], "dir": S["dir"], "line": S["line"],
                        "signal": {k: v for k, v in sig.items() if k != "end"} if sig else None,
                        "plan": S["plan"], "risk": S["risk"], "trade": S["trade"], "missed": bool(S["missed"])}
        return out
