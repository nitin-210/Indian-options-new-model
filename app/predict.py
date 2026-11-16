"""Dashboard predictions: one card per index (NIFTY, SENSEX, NIFTY BANK) and per picked stock.

Index cards come straight from the engine's tested rule (opening-range breakout in yesterday's direction, held to
the stop or 15:00) plus the day-type rating (daytype.py). T1 / T2 / T3 are reference levels at 1R / 2R / 3R: in
testing, holding to 15:00 beat every fixed target, so they are shown for information, not as exits.

Stock picks: the day's stocks most likely to make a big move, ranked by the size of their first 15 minutes and their
gap compared with their own normal daily range (validated: the top 10% of stocks by this score ended the day with a
big move 50% of the time, against 29% for all stocks). The breakout levels shown for stocks are reference levels
only: the plain opening-range breakout LOST money on stocks in testing (about -0.01R a trade, 2017-2026). The tested
stock signal (v2's model-scored patterns) is not in the app yet.

Stock short lists (dashboard): of the day's breaks, only the ones still running, each with its trade -
"picks" (right column: a top-10% volatile stock that broke out, at most 3 at a time) and "movers" (left column: a
top-10 gainer that broke up / loser that broke down, not a pick, at most 5 at a time). The trade: the at-the-money
option, stop at the far side of the range, target 2R; it leaves the list when it ends (stop, target, a 5-minute
candle closing back inside the range, or 15:00). Breaks after 14:00 are not listed. Tested on 55 large caps,
Dec 2022 - Nov 2026 (research: /home/claude/ml/stocklist/sim.py, /home/claude/ml/lab/shortlist_check.py): picks
about +0.01 to +0.03R a trade before option costs, movers about -0.01R; option costs are about 0.10R, so neither
is profitable yet - paper trade first. Running at once: picks about 2.5 on an average day's busiest moment (4 on
the busiest tenth of days), movers about 4 (5).

Signals only. Nothing is ever placed.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import date, datetime, timedelta

from . import bs, odds
from .breakouts import BreakoutScanner
from .costs import slip
from .daytype import DayTypeModels, daily_context, live_features, rating
from .instruments import INDEX_META
from .strategy import Quote, breakout_margin
from .timeutil import IST, hm, ist

log = logging.getLogger(__name__)
BIG_PTS = {"NIFTY": 150.0, "SENSEX": 500.0}
OTHER = {"NIFTY": "SENSEX", "SENSEX": "NIFTY", "BANKNIFTY": "NIFTY"}
MAX_LIVE = {"picks": 3, "movers": 5}   # running trades per stock list (first come; a slot frees on exit)
FRICTION_MAX = 0.15       # leave out a stock trade whose option's spread + slippage (in and out) is over 15% of its
                          # expected gain at the 2R target (e.g. a Rs 0.70 option with a Rs 0.05 tick)
STOCK_BIG_ATR = 0.6       # a stock's "big day" = open -> 15:00 move >= 0.6 x its average daily range


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


class Predictor:
    def __init__(self, engine):
        self.e = engine
        try:
            self.models = DayTypeModels()
        except Exception as ex:  # pragma: no cover - the app still runs without ratings
            log.warning("Day-type models not loaded: %s", ex)
            self.models = None
        self._ctx: dict[tuple[str, date], object] = {}
        self._ratings: dict[tuple[str, date, str], dict] = {}
        self._breadth: deque = deque(maxlen=400)          # (time, share of stocks above their open)
        self._vix_open: dict[date, float] = {}
        self._picks: list[dict] = []
        self._picks_at: datetime | None = None
        self.pick_tokens: set[str] = set()
        self._rank: dict[str, float] = {}                    # each stock's score percentile today (for stars)
        self.scanner = BreakoutScanner(engine)
        self._st_day: date | None = None
        self._ok: dict[tuple, bool] = {}
        self._pick_seen: set[str] = set()                    # every stock that was a pick at some point today
        self._pick_toks: set[str] = set()
        engine.stock_signals = {}                            # (name, side) -> the listed trade (for the paper desk)

    # ------------------------------------------------------------------ shared inputs
    def breadth(self) -> float | None:
        up = n = 0
        for st in self.e.movers.stocks.values():
            if st.ltp > 0 and st.open > 0:
                n += 1
                up += st.ltp > st.open
        return up / n if n >= 20 else None

    def _breadth_ago(self, now: datetime, minutes: int) -> float | None:
        target = now - timedelta(minutes=minutes)
        best = None
        for t, v in self._breadth:
            if t <= target:
                best = v
        return best

    def _daily_ctx(self, key: str, today: date):
        k = (key, today)
        if k not in self._ctx:
            bars = [b for b in (self.e.hist_bars.get(key) or []) if b.start.astimezone(IST).date() < today]
            rows = [(d, v[0].o, max(b.h for b in odds.continuous(d, v)), min(b.l for b in odds.continuous(d, v)), v[-1].c)
                    for d, v in odds.regular_sessions(bars).items()]
            self._ctx[k] = daily_context(rows) if rows else None
        return self._ctx[k]

    def _rating(self, key: str, now: datetime, side: str | None) -> dict | None:
        if self.models is None:
            return None
        hhmm = now.astimezone(IST).strftime("%H:%M")
        cp = self.models.checkpoint_for(hhmm)
        if cp is None:
            return {"pending": True, "text": f"Move-size rating starts at {self.models.checkpoints[0]} IST "
                                             "(it needs the opening range)."}
        today = now.astimezone(IST).date()
        ck = (key, today, cp)
        if ck not in self._ratings:
            st = self.e.idx[key]
            cut = ist(today, hm(cp))
            bars = [b for b in st.c5.bars if b.start.astimezone(IST).date() == today
                    and b.start + timedelta(minutes=5) <= cut]
            ctx = self._daily_ctx(key, today)
            if not bars or ctx is None or bars[0].start.astimezone(IST).strftime("%H:%M") != "09:15":
                return {"pending": True, "text": "Rating needs today's candles from 09:15 and the daily history "
                                                 "(still loading)." if ctx is None else
                        "Rating needs today's candles from 09:15 (the app started late)."}
            other = self.e.idx.get(OTHER[key])
            o_ret = math.log(other.ltp / other.day_open) if other and other.ltp > 0 and other.day_open > 0 else None
            if key == "BANKNIFTY":
                nif = self.e.idx["NIFTY"]
                big = BIG_PTS["NIFTY"] * (st.ltp / nif.ltp) if nif.ltp > 0 else 340.0
                dte_key = "NIFTY"
            else:
                big = BIG_PTS[key]
                dte_key = key
            vix = self.e.vix["ltp"] or None
            br = self.breadth()
            feats = live_features(bars, ctx, is_nifty=0.0 if key == "SENSEX" else 1.0, big_pts=big, vix=vix,
                                  vix_open=self._vix_open.get(today), breadth=br,
                                  breadth_1h=self._breadth_ago(now, 65),
                                  other_ret_open=o_ret, dte=(odds.weekly_expiry_for(dte_key, today) - today).days,
                                  weekday=today.weekday())
            if feats is None:
                return {"pending": True, "text": "Not enough data for the rating yet."}
            p = self.models.predict(cp, feats)
            self._ratings[ck] = {"cp": cp, "p": p, "approx": key == "BANKNIFTY"}
        r = self._ratings[ck]
        out = rating(r["p"], side)
        auc = {"09:30": 0.68, "09:45": 0.69, "10:15": 0.73, "11:00": 0.76, "11:45": 0.79, "12:30": 0.83,
               "13:15": 0.86, "14:00": 0.87}.get(r["cp"], 0.75)
        out.update({"cp": r["cp"], "approx": r["approx"], "auc": auc,
                    "text": f"Checked at {r['cp']} IST. Model skill at this time: AUC {auc:.2f} (0.5 = coin flip)."
                            + (" NIFTY BANK uses the NIFTY model (approximate)." if r["approx"] else "")})
        return out

    # ------------------------------------------------------------------ option helpers
    def _opt_quote(self, c) -> Quote:
        return self.e.quotes.get(c.token, Quote()) if c else Quote()

    def _premium_at(self, c, S: float, now: datetime, iv: float | None) -> float | None:
        if not c or not iv:
            return None
        T = bs.years_to_expiry(now, c.expiry)
        return _r(max(0.05, bs.price(S, c.strike, T, self.e.s.risk_free_rate_pct / 100, iv, c.opt)))

    def _opt_view(self, c, spot: float, now: datetime, levels: dict, step: float | None = None) -> dict | None:
        if c is None:
            return None
        q = self._opt_quote(c)
        px = q.ask or q.ltp
        iv = None
        if q.ltp > 0 and spot > 0:
            T = bs.years_to_expiry(now, c.expiry)
            iv = bs.implied_vol(q.mid if q.mid > 0 else q.ltp, spot, c.strike, T, self.e.s.risk_free_rate_pct / 100, c.opt)
        if iv is None:
            iv = (self.e.vix["ltp"] or 14.0) / 100 * 1.05
        prem = {k: self._premium_at(c, v, now, iv) for k, v in levels.items() if v}
        ref = levels.get("entry") or spot
        money = None
        if step and ref:
            n = round((c.strike - ref) / step) * (1 if c.opt == "CE" else -1)
            money = "ATM" if n == 0 else (f"{n} OTM" if n > 0 else f"{-n} ITM")
        return {"label": c.label(), "strike": c.strike, "opt": c.opt, "expiry": c.expiry.isoformat(), "lot": c.lot,
                "money": money,
                "ltp": _r(q.ltp) if q.ltp else None, "bid": _r(q.bid) if q.bid else None, "ask": _r(q.ask) if q.ask else None,
                "iv": _r(iv * 100, 1) if iv else None, "lot_cost": round(px * c.lot) if px else None, "at": prem}

    # ------------------------------------------------------------------ index cards
    def index_card(self, key: str, now: datetime) -> dict:
        e = self.e
        st = e.idx[key]
        meta = INDEX_META[key]
        s = e.s
        o = st.orng
        today = now.astimezone(IST).date()
        u = e.universes.get(key)
        exp = st.expiry
        card = {"key": key, "label": meta["label"], "tested": meta["tested"], "ltp": _r(st.ltp),
                "chg": _r(st.ltp - st.prev_close) if st.prev_close else None,
                "chg_pct": _r((st.ltp / st.prev_close - 1) * 100) if st.prev_close and st.ltp else None,
                "phase": st.phase, "or": {"high": _r(o.high), "low": _r(o.low), "mid": _r(o.mid),
                                          "complete": o.complete} if o else None,
                "yday": None, "status": "", "headline": "", "plan": None, "watch": [], "reasons": [], "rating": None,
                "expiry": exp.isoformat() if exp else None}
        y = st.yday or {}
        if y:
            card["yday"] = {"side": y.get("side"), "range_atr": _r(y.get("range_atr")),
                            "chg_pct": _r((y["close"] / y["open"] - 1) * 100) if y.get("open") else None}
        allowed = e.allowed_sides(st)
        side_now = st.plan["side"] if st.plan else (st.signal.side if st.signal else
                                                    (next(iter(allowed)) if allowed and len(allowed) == 1 else None))
        card["rating"] = self._rating(key, now, side_now)
        buf = breakout_margin(o, s) if o else 0.0
        if card["or"]:
            card["or"]["buf"] = _r(buf)
        # ---- status line
        ph = st.phase
        status = {"before": "Waiting for the open", "forming": "Opening range forming (09:15-09:30)",
                  "waiting": "No trade yet: watching for a breakout", "signal": "SIGNAL", "expired": "No trade today",
                  "skipday": "No trade today (after a wide-range day)", "closed": "Session over",
                  "holiday": "Market holiday", "missed": "Signal came while the app was off"}.get(ph, ph)
        card["status"] = status
        # ---- reasons (shared)
        R = card["reasons"]
        if o and o.complete:
            R.append(f"Opening range {o.low:,.2f} - {o.high:,.2f} ({o.high - o.low:,.0f} pts). A 5-minute close must "
                     f"clear it by {buf:,.0f} pts (0.2x the width).")
        if y.get("side"):
            R.append(f"Yesterday closed {'up' if y['side'] == 'CE' else 'down'} "
                     f"({card['yday']['chg_pct']:+.2f}%), so only {'CE (calls)' if y['side'] == 'CE' else 'PE (puts)'} "
                     "count today." if s.trend_filter == "yesterday" else "Trend filter off: both sides count.")
        if y.get("range_atr") is not None:
            ra = y["range_atr"]
            R.append(f"Yesterday's range was {ra:.2f}x normal" + (
                " - a wide-range day, so the tested model sits today out." if ra > s.wide_day_atr and s.skip_after_wide_day
                else "."))
        # ---- plan (signal) or watch levels
        if st.plan:
            p = st.plan
            sd = 1 if p["side"] == "CE" else -1
            entry, stop, risk = p["entry_index"], p["stop_index"], p["risk_pts"]
            tg = {f"T{i}": _r(entry + sd * i * risk) for i in (1, 2, 3)}
            c = u.by_token.get(p["contract"]["token"]) if u else None
            k_otm = (c.strike + sd * u.step(c.expiry)) if (u and c) else None
            c_otm = u.contract(c.expiry, k_otm, c.opt) if (u and c and k_otm) else None
            lv = {"entry": entry, "stop": stop, **tg}
            sig_t = datetime.fromisoformat(p["signal_time"]).astimezone(IST).strftime("%H:%M")
            card["headline"] = f"BUY {p['side']}  ·  {p['contract']['label']}"
            card["plan"] = {
                "side": p["side"], "signal_time": sig_t, "entry": _r(entry), "stop": _r(stop), "risk_pts": _r(risk),
                "targets": tg,
                "exit": (f"Hold to {s.time_exit} IST unless the stop is hit. After +1R (T1) the stop follows 1R behind "
                         "the best price (trailing exit); T2-T3 are reference levels." if p.get("exit_mode") == "trail" else
                         f"Hold to {s.time_exit} IST unless the stop is hit (T1-T3 are reference levels)."),
                "exit_mode": p.get("exit_mode"),
                "stop_now": _r((st.trail or {}).get("stop")) if (st.trail or {}).get("active") else None,
                "premium_now": _r(p.get("premium_ltp")), "entry_limit": _r(p.get("entry_limit")),
                "skip_above": _r(p.get("max_entry_premium")), "prem_stop": _r(p.get("prem_stop")),
                "lots": p.get("lots"), "qty": p.get("qty"), "lot": p.get("lot"), "loss_at_stop": _r(p.get("loss_at_stop"), 0),
                "one_lot_loss": _r(p.get("one_lot_loss"), 0),
                "risk": (st.risk or {}).get("verdict"), "risk_reasons": (st.risk or {}).get("reasons") or [],
                "atm": self._opt_view(c, st.ltp, now, lv, u.step(c.expiry) if (u and c) else None),
                "otm": self._opt_view(c_otm, st.ltp, now, lv, u.step(c.expiry) if (u and c) else None),
                "trade": None}
            if st.trade:
                tr = st.trade
                card["plan"]["trade"] = {"status": tr.get("status"), "entry_fill": _r(tr.get("entry_fill")),
                                         "pnl": _r(tr.get("pnl"), 0), "mark": _r((tr.get("mark") or {}).get("pnl"), 0)
                                         if isinstance(tr.get("mark"), dict) else None}
            trig, side = None, p["side"]
            if o and o.complete:
                trig = (o.high + buf) if side == "CE" else (o.low - buf)
                card["plan"]["trigger"] = _r(trig)
            edge = (o.high if side == "CE" else o.low) if o else None
            R.insert(0, f"The 5-minute candle ending {sig_t} IST closed at {entry:,.2f}"
                        + (f", {'above' if side == 'CE' else 'below'} the trigger {trig:,.2f} (the range "
                           f"{'high' if side == 'CE' else 'low'} {edge:,.2f} {'+' if side == 'CE' else '−'} 0.2 × the "
                           f"{o.high - o.low:,.0f}-pt range = {buf:,.0f} pts). Earlier closes that only just crossed the "
                           f"range did not clear the trigger" if trig is not None else ", beyond the opening range by the rule's margin")
                        + f". It is in yesterday's direction. Stop on the {'far side' if s.stop_rule == 'far' else 'middle'} "
                        f"of the range at {stop:,.2f} ({risk:,.0f} pts = 1R).")
        elif o and o.complete and ph in ("waiting", "forming"):
            for side in (sorted(allowed) if allowed else ["CE", "PE"]):
                sd = 1 if side == "CE" else -1
                trig = (o.high + buf) if side == "CE" else (o.low - buf)
                stop = (o.low if side == "CE" else o.high) if s.stop_rule == "far" else o.mid
                risk = abs(trig - stop)
                tg = {f"T{i}": _r(trig + sd * i * risk) for i in (1, 2, 3)}
                c = None
                if u and exp:
                    c = u.contract(exp, u.atm(trig, exp), side)
                card["watch"].append({"side": side, "trigger": _r(trig), "stop": _r(stop), "risk_pts": _r(risk),
                                      "targets": tg, "distance": _r(trig - st.ltp),
                                      "option": self._opt_view(c, st.ltp, now, {"entry": trig, "stop": stop, **tg},
                                                               u.step(exp) if (u and c) else None)})
            card["headline"] = ("Watching: " + " / ".join(
                f"{w['side']} above {w['trigger']:,.2f}" if w["side"] == "CE" else f"{w['side']} below {w['trigger']:,.2f}"
                for w in card["watch"])) if card["watch"] else status
        else:
            card["headline"] = status
        if not meta["tested"]:
            R.append("NIFTY BANK uses the same rule but was not part of the backtested system (monthly options only).")
        rt = card["rating"]
        if rt and not rt.get("pending"):
            R.append(f"Move-size model: {rt['label']} (big our way {rt['p_big_with']:.0%}, small {rt['p_small']:.0%}).")
        return card

    # ------------------------------------------------------------------ stock picks
    def _stock_or(self, name: str, today: date):
        c1, c5 = self.e.stock_bars.get(name, (None, None))
        if not c1:
            return None, []
        first = [b for b in c1.bars if b.start.astimezone(IST).date() == today and b.start < ist(today, hm("09:30"))]
        fives = [b for b in (c5.bars if c5 else []) if b.start.astimezone(IST).date() == today]
        if not first or first[0].start.astimezone(IST).strftime("%H:%M") > "09:17":
            return None, fives
        return (max(b.h for b in first), min(b.l for b in first)), fives

    def _atr(self, name: str, today: date) -> float | None:
        cal = self.e.cal   # regular sessions only: a Muhurat candle (about an hour) would shrink the normal range
        rows = [r for r in (self.e.stock_daily.get(name) or []) if r[0] < today and cal.is_trading_day(r[0])]
        if len(rows) < 14:
            return None
        return sum(r[2] - r[3] for r in rows[-14:]) / 14

    def picks(self, now: datetime, n: int = 10) -> list[dict]:
        e = self.e
        today = now.astimezone(IST).date()
        after_or = now >= ist(today, hm("09:30"))
        rows = []
        for name, st in e.movers.stocks.items():
            r = self._row(name, st, today, after_or)
            if r:
                rows.append(r)
        self._scores = sorted(r["score"] for r in rows)
        if not rows:
            return []
        rows.sort(key=lambda r: r["turnover"])
        cut = rows[len(rows) // 4]["turnover"] if len(rows) >= 8 else 0.0      # drop the least traded quarter
        liquid = [r for r in rows if r["turnover"] >= cut]
        ranked = sorted(rows, key=lambda r: r["score"])
        pct = {r["name"]: (i + 1) / len(ranked) for i, r in enumerate(ranked)}
        self._rank = pct
        top = sorted(liquid, key=lambda r: -r["score"])[:n]
        out = []
        for r in top:
            out.append(self._stock_card(r, pct[r["name"]], now, after_or))
        return out

    def _row(self, name: str, st, today: date, after_or: bool) -> dict | None:
        e = self.e
        if st.ltp <= 0 or st.prev_close <= 0 or st.open <= 0 or name not in e.movers.universes:
            return None
        atr = self._atr(name, today)
        orr, fives = self._stock_or(name, today) if after_or else (None, [])
        gap = st.open - st.prev_close
        if orr is None and after_or:
            orr = (st.high, st.low) if st.high > 0 and st.low > 0 else None   # app started late: day range so far
            approx_or = True
        else:
            approx_or = False
        w = (orr[0] - orr[1]) if orr else 0.0
        if atr:
            score = (w / atr if orr else 0.0) + 0.5 * abs(gap) / atr
        else:
            score = (w / st.ltp * 50 if orr else 0.0) + 0.5 * abs(gap) / st.ltp * 50
        turnover = st.ltp * st.vol / 1e7 if st.vol else 0.0
        return {"name": name, "st": st, "atr": atr, "or": orr, "approx_or": approx_or, "fives": fives,
                "gap": gap, "score": score, "turnover": turnover}

    def selected(self, now: datetime) -> dict | None:
        """The card for the stock open on the chart when it is not one of the picks (same rules as the picks)."""
        e = self.e
        name = e.chart_stock
        if not name or any(p["name"] == name for p in self._picks):
            return None
        st = e.movers.stocks.get(name)
        if not st:
            return None
        today = now.astimezone(IST).date()
        after_or = now >= ist(today, hm("09:30"))
        r = self._row(name, st, today, after_or)
        if not r:
            return None
        sc = getattr(self, "_scores", None) or [r["score"]]
        pct = sum(1 for x in sc if x <= r["score"]) / len(sc)
        card = self._stock_card(r, pct, now, after_or)
        card["in_picks"] = False
        return card

    def _stock_card(self, r: dict, pct: float, now: datetime, after_or: bool) -> dict:
        e = self.e
        st, name = r["st"], r["name"]
        today = now.astimezone(IST).date()
        stars = 3 if pct >= 0.9 else (2 if pct >= 0.75 else 1)
        star_text = {3: "Top 10% today: in testing, these stocks made a big move on 50% of days (normal: 29%).",
                     2: "Top 25% today: in testing, about 37% of these made a big move (normal: 29%).",
                     1: "Ordinary mover: about 22-29% make a big move."}[stars]
        card = {"name": name, "ltp": _r(st.ltp), "pct": _r(st.pct), "gap_pct": _r(r["gap"] / st.prev_close * 100),
                "score": _r(r["score"]), "rank_pct": _r(pct * 100, 0), "stars": stars, "star_text": star_text,
                "atr": _r(r["atr"]), "turnover_cr": _r(r["turnover"], 1), "or": None, "levels": None, "breakout": None,
                "option": None, "reasons": [], "tested": False, "lot": st.info.lot}
        R = card["reasons"]
        atr = r["atr"]
        gap_pct = card["gap_pct"] or 0.0
        if r["or"]:
            h, lo = r["or"]
            w = h - lo
            card["or"] = {"high": _r(h), "low": _r(lo), "approx": r["approx_or"]}
            R.append(f"First 15 minutes: {lo:,.2f} - {h:,.2f}" + (f" = {w / atr:.2f}x its normal daily range" if atr else
                                                               f" ({w / st.ltp * 100:.2f}% of price)")
                     + (" (day range so far; the app started after 09:30)" if r["approx_or"] else "") + ".")
            buf = 0.2 * w
            up, dn = h + buf, lo - buf
            brk = None
            if r["fives"]:
                # the first completed 5-minute candle after 09:30 that closes beyond the trigger (as for the indices),
                # with signals from candles closing up to 14:00
                for b5 in r["fives"]:
                    t = b5.start.astimezone(IST)
                    end = t + timedelta(minutes=5)
                    if t < ist(today, hm("09:30")) or not b5.closed or end > ist(today, hm("14:00")):
                        continue
                    if b5.c > up:
                        brk = {"side": "CE", "time": end.strftime("%H:%M"), "close": _r(b5.c)}
                        break
                    if b5.c < dn:
                        brk = {"side": "PE", "time": end.strftime("%H:%M"), "close": _r(b5.c)}
                        break
            card["breakout"] = brk
            lean = brk["side"] if brk else ("CE" if (st.ltp - (h + lo) / 2) > 0 else "PE")
            sd = 1 if lean == "CE" else -1
            entry = brk["close"] if brk else (up if lean == "CE" else dn)
            stop = lo if lean == "CE" else h
            risk = abs(entry - stop)
            tg = {f"T{i}": _r(entry + sd * i * risk) for i in (1, 2, 3)}
            nxt = None
            if not brk and after_or:
                m = now.astimezone(IST)
                mins = m.hour * 60 + m.minute
                k5 = (mins // 5 + 1) * 5                           # the next 5-minute close after now
                if k5 <= 14 * 60:
                    nxt = f"{k5 // 60:02d}:{k5 % 60:02d}"
            card["levels"] = {"side": lean, "entry": _r(entry), "stop": _r(stop), "risk": _r(risk), "targets": tg,
                              "up": _r(up), "dn": _r(dn), "next_check": nxt}
            u = e.movers.universes.get(name)
            if u:
                exp, _ = u.choose_expiry(today)
                if exp:
                    # the strike at the money where the trade starts (the breakout close, or the trigger before one)
                    c = u.contract(exp, u.atm(entry, exp), lean)
                    card["option"] = self._opt_view(c, st.ltp, now, {"entry": entry, "stop": stop, **tg}, u.step(exp))
            if brk:
                at = (f" At that moment it was {(brk['close'] / st.prev_close - 1) * 100:+.2f}% on the day"
                      if st.prev_close > 0 else "")
                R.append(f"Broke {'above' if brk['side'] == 'CE' else 'below'} its range: the 5-minute candle ending "
                         f"{brk['time']} closed at {brk['close']:,.2f}, beyond the trigger.{at}.")
                # what happened next, from the 5-minute candles after the signal
                t_sig = ist(today, hm(brk["time"]))
                best, out = 0.0, {"status": "open", "time": None, "r": None}
                for b5 in r["fives"]:
                    if b5.start < t_sig:
                        continue
                    hit_stop = (b5.l <= stop) if lean == "CE" else (b5.h >= stop)
                    if hit_stop:
                        out = {"status": "stopped", "time": (b5.start + timedelta(minutes=5)).astimezone(IST).strftime("%H:%M"),
                               "r": -1.0}
                        break
                    fav = ((b5.h - entry) if lean == "CE" else (entry - b5.l)) / risk if risk > 0 else 0.0
                    best = max(best, fav)
                    if b5.start.astimezone(IST).strftime("%H:%M") >= "14:55" and b5.closed:
                        out = {"status": "closed", "time": "15:00", "r": round((b5.c - entry) * sd / risk, 2) if risk else None}
                        break
                if out["status"] == "open" and r["fives"] and risk:
                    out["r"] = round((st.ltp - entry) * sd / risk, 2)
                out["best_r"] = round(best, 2)
                card["outcome"] = out
                if out["status"] == "stopped":
                    R.insert(0, f"Stopped out at {out['time']} IST: the breakout reversed (−1R)"
                                + (f", after going {best:.1f}R in its favour" if best >= 0.5 else "") + ".")
                elif out["status"] == "closed":
                    R.insert(0, f"Held to 15:00: {out['r']:+.2f}R (best {best:.1f}R).")
            else:
                trig = up if lean == "CE" else dn
                R.append(f"No breakout yet: a 5-minute candle closing {'above' if lean == 'CE' else 'below'} {trig:,.2f} "
                         f"is the breakout" + (f" (next candle closes at {nxt} IST)" if nxt else "")
                         + f". The {lean} side is shown because it sits {'above' if lean == 'CE' else 'below'} the middle of its range.")
        else:
            R.append("Before 09:30: ranked by its opening gap." if not after_or else "Waiting for its first candles.")
        if abs(gap_pct) >= 0.3:
            R.append(f"Gapped {gap_pct:+.2f}% at the open" + (f" ({abs(r['gap']) / atr:.2f}x its normal range)." if atr else "."))
        R.append(star_text)
        R.append("Levels are for reference only: the plain breakout rule lost money on stocks in testing. "
                 "The tested stock model is not in the app yet.")
        return card

    # ------------------------------------------------------------------ stock short lists
    def stock_trades(self, now: datetime, brks: list[dict]) -> dict:
        e = self.e
        today = now.astimezone(IST).date()
        if self._st_day != today:
            self._st_day, self._pick_seen, self._ok = today, set(), {}
        if now >= ist(today, hm("09:30")):
            self._pick_seen |= {p["name"] for p in self._picks}
        lists: dict[str, list[dict]] = {"picks": [], "movers": []}
        skipped = {"picks": 0, "movers": 0}
        thin = {"picks": 0, "movers": 0}
        for b in sorted((b for b in brks if not b["late"]), key=lambda b: (b["time"], b["name"])):
            which = "picks" if b["name"] in self._pick_seen else ("movers" if b["top_mover"] else None)
            if which is None:
                continue
            if not self._tradeable(b, which, now, today):
                thin[which] += 1
                continue
            L = lists[which]
            running = [x for x in L if x["exit"] is None or x["exit"]["time"] > b["time"]]
            if len(running) < MAX_LIVE[which]:
                L.append(b)
            else:
                skipped[which] += 1
        out, signals = {}, {}
        for which, L in lists.items():
            live = []
            for b in reversed(L):                                   # newest first
                if b["exit"] is not None:
                    continue
                row = self._stock_trade_row(b, which, now, today)
                if row:
                    live.append(row)
                    signals[(b["name"], b["side"])] = row
            done = [{"name": b["name"], "side": b["side"], "time": b["time"], "exit": b["exit"]}
                    for b in L if b["exit"] is not None]
            out[which] = {"live": live, "done": done, "skipped": skipped[which], "thin": thin[which], "max": MAX_LIVE[which],
                          "done_r": round(sum(d["exit"]["r"] for d in done), 2)}
        broke = {b["name"] for b in brks}
        t = now.astimezone(IST)
        out["waiting"] = [p["name"] for p in self._picks if p["name"] not in broke] if t.strftime("%H:%M") < "14:00" else []
        e.stock_signals = signals
        return out

    def _tradeable(self, b: dict, which: str, now: datetime, today: date) -> bool:
        """Is the break's option worth trading? Decided once per break (when it is first seen), so a row never
        flickers on and off: spread + slippage in and out must be at most FRICTION_MAX of the expected gain."""
        key = (b["name"], b["side"], b["time"])
        if key not in self._ok:
            row = self._stock_trade_row(b, which, now, today)
            self._ok[key] = bool(row and row["friction_ok"])
            if row and not row["friction_ok"]:
                log.info("Left %s %s off the %s list: option too thin (spread + slippage %.2f against an expected "
                         "gain of %.2f at the target).", b["name"], b["side"], which, row["friction"],
                         row["gain"] or 0)
        return self._ok[key]

    def _stock_trade_row(self, b: dict, which: str, now: datetime, today: date) -> dict | None:
        e = self.e
        name, side = b["name"], b["side"]
        st = e.movers.stocks.get(name)
        u = e.movers.universes.get(name)
        if not st or not u:
            return None
        exp, _ = u.choose_expiry(today)
        c = u.contract(exp, u.atm(b["close"], exp), side) if exp else None
        if c is None:
            return None
        ov = self._opt_view(c, st.ltp, now, {"now": st.ltp, "entry": b["close"], "stop": b["stop"],
                                             "target": b["target"]}, u.step(exp))
        at = ov.get("at") or {}
        buy = ov.get("ask") or ov.get("ltp")
        est = buy is None
        if est:
            buy = at.get("now")
        ce = side == "CE"
        tick = c.tick or 0.05
        bid, ask = ov.get("bid"), ov.get("ask")
        spread = (ask - bid) if (bid and ask and ask > bid) else max(tick, 0.012 * (buy or 0))   # no quote: ~0.6% a side
        friction = spread + 2 * slip(tick, e.s)
        gain = (at.get("target") or 0) - (at.get("entry") or 0)
        why = (f"Top 10% most volatile, broke {'up' if ce else 'down'}" if which == "picks" else
               f"#{b['mover_rank']} {'gainer' if ce else 'loser'} when it broke {'up' if ce else 'down'}")
        return {"name": name, "side": side, "list": which, "time": b["time"], "entry": b["close"], "stop": b["stop"],
                "target": b["target"], "range_high": b["high"], "range_low": b["low"], "ltp": b["ltp"], "pct": b["pct"],
                "r": b["r"], "since_pct": b["since_pct"], "stars": b["stars"], "mover_rank": b["mover_rank"],
                "token": c.token, "contract": f"{name} {c.strike:,.0f} {side} {c.expiry:%d %b}", "strike": c.strike,
                "expiry": c.expiry.isoformat(), "lot": c.lot, "buy": _r(buy), "buy_est": est,
                "lot_cost": round(buy * c.lot) if buy else None,
                "lot_risk": round(c.lot * max(0.0, buy - at["stop"])) if buy and at.get("stop") is not None else None,
                "prem_stop": _r(at.get("stop")), "prem_target": _r(at.get("target")), "why": why,
                "friction": _r(friction), "gain": _r(gain), "friction_ok": gain > 0 and friction <= FRICTION_MAX * gain,
                "exit_time": ist(today, hm("15:00")).isoformat()}

    # ------------------------------------------------------------------ the trades to take
    def trades_today(self, now: datetime) -> list[dict]:
        """Today's signals from the two TESTED strategies on NIFTY and SENSEX - the only things on the dashboard
        that are trades. (NIFTY BANK, stock picks, range breaks and movers are for watching.)"""
        e = self.e
        out = []

        def row(key, strategy, p, risk, trade, missed=None, trail=None):
            label = INDEX_META[key]["label"]
            if missed:
                return {"key": key, "index": label, "strategy": strategy, "time": missed["time"], "side": missed["side"],
                        "status": "missed", "text": "Came while the app was off: don't chase it."}
            sd = 1 if p["side"] == "CE" else -1
            stop_now = (trail or {}).get("stop") if (trail or {}).get("active") else None
            tr = trade or {}
            live = tr.get("live_pnl")
            if isinstance(live, dict):
                live = live.get("pnl")
            r = {"key": key, "index": label, "strategy": strategy,
                 "time": datetime.fromisoformat(p["signal_time"]).astimezone(IST).strftime("%H:%M"),
                 "side": p["side"], "contract": p["contract"]["label"], "buy_at": _r(p.get("entry_fill")),
                 "limit": _r(p.get("entry_limit")), "skip_above": _r(p.get("max_entry_premium")),
                 "entry_index": _r(p["entry_index"]), "stop_index": _r(p["stop_index"]), "stop_now": _r(stop_now),
                 "stop_premium": _r(p.get("exit_fill_stop")), "risk_pts": _r(p.get("risk_pts")),
                 "trail_from": _r(p["entry_index"] + sd * p["risk_pts"]) if p.get("exit_mode") == "trail" else None,
                 "exit_mode": p.get("exit_mode"), "exit_time": datetime.fromisoformat(p["exit_time"]).astimezone(IST).strftime("%H:%M"),
                 "lots": p.get("lots"), "qty": p.get("qty"), "lot": p.get("lot"),
                 "risk_rupees": _r(p.get("loss_at_stop") if p.get("lots") else p.get("one_lot_loss"), 0),
                 "verdict": (risk or {}).get("verdict"), "reasons": (risk or {}).get("reasons") or (risk or {}).get("blocks") or [],
                 "max_loss": e.s.max_loss_per_trade,
                 "status": tr.get("status") or "planning", "kind": tr.get("kind"),
                 "pnl": _r(tr.get("pnl"), 0) if tr.get("status") == "closed" else None,
                 "live_pnl": _r(live, 0) if tr.get("status") == "open" else None,
                 "exit_reason": tr.get("exit_reason"),
                 "exit_at": datetime.fromisoformat(tr["exit_time"]).astimezone(IST).strftime("%H:%M") if tr.get("exit_time") else None}
            return r

        for key in ("NIFTY", "SENSEX"):
            st = e.idx.get(key)
            if st is None:
                continue
            if st.plan and not st.plan.get("error"):
                out.append(row(key, "Opening-range breakout", st.plan, st.risk, st.trade, trail=st.trail))
            elif st.missed is not None and st.signal is not None:
                out.append(row(key, "Opening-range breakout", None, None, None,
                               missed={"time": st.signal.time.astimezone(IST).strftime("%H:%M"), "side": st.signal.side}))
            S = e.supertrend.state.get(key) or {}
            if S.get("plan"):
                out.append(row(key, "Supertrend flip", S["plan"], S.get("risk"), S.get("trade")))
            elif S.get("missed") and S.get("signal"):
                out.append(row(key, "Supertrend flip", None, None, None,
                               missed={"time": S["signal"]["time"], "side": S["signal"]["side"]}))
        out.sort(key=lambda r: r["time"])
        return out

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        e = self.e
        now = e.clock()
        today = now.astimezone(IST).date()
        if e.vix["ltp"] and today not in self._vix_open and now >= ist(today, hm("09:15")):
            self._vix_open[today] = e.vix["ltp"]
        br = self.breadth()
        if br is not None and (not self._breadth or (now - self._breadth[-1][0]).total_seconds() >= 60):
            self._breadth.append((now, br))
        if self._picks_at is None or (now - self._picks_at).total_seconds() >= 10 or now < self._picks_at:
            self._picks = self.picks(now)
            self._picks_at = now
            toks = set()
            for p in self._picks:
                u = e.movers.universes.get(p["name"])
                if not u or not p.get("ltp"):
                    continue
                exp, _ = u.choose_expiry(today)
                if not exp:
                    continue
                k = u.atm((p.get("levels") or {}).get("entry") or p["ltp"], exp)   # the strike the card shows
                for opt in ("CE", "PE"):
                    c = u.contract(exp, k, opt)
                    if c:
                        toks.add(c.token)
            self._pick_toks = toks
        brks = self.scanner.scan(now, self._picks, self._rank)
        lists = self.stock_trades(now, brks)
        toks = self._pick_toks | {r["token"] for k in ("picks", "movers") for r in lists[k]["live"]}
        if toks != self.pick_tokens:
            self.pick_tokens = toks
            e.pick_tokens = toks
            e._recentre(force=True)
        return {"now": now.isoformat(), "time_ist": now.astimezone(IST).strftime("%H:%M:%S"),
                "indices": {k: self.index_card(k, now) for k in e.idx},
                "picks": self._picks, "selected": self.selected(now), "breadth": _r(br, 3),
                "breakouts": brks, "stock_trades": lists,
                "trades": self.trades_today(now),
                "settings": {"stop_rule": e.s.stop_rule, "skip_after_wide_day": e.s.skip_after_wide_day,
                             "trend_filter": e.s.trend_filter, "time_exit": e.s.time_exit}}
