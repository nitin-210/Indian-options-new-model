"""The engine: turns ticks into candles, the option chain, signals, plans,
risk checks and paper trades, and publishes snapshots to the browser.

All times come from `clock()` (real time when live, simulated time in demo or
replay), so the same code runs a live session and a replay.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from . import bs, odds, risk as riskmod
from .candles import Bar, CandleBuilder
from .config import DATA_DIR, Settings
from .costs import round_trip, slip
from .instruments import INDEX_META, VIX_META, Contract, OptionUniverse
from .journal import Journal
from .movers import FnoStock, Movers
from .strategy import (FIVE, OpeningRange, Quote, Signal, breakout_margin, compare_strikes, find_signal,
                       index_levels, make_plan,
                       opening_range)
from .timeutil import AUCTION_FROM, INDEX_AUCTION, IST, UK, Calendar, fmt_both, fmt_day, hm, humanize_delta, ist

log = logging.getLogger(__name__)


class Hub:
    """Fan-out of server-sent events to every open browser tab."""

    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=300)
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)

    def publish(self, event: str, data) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"
        for q in list(self.queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)


@dataclass
class IndexState:
    key: str
    meta: dict
    c1: CandleBuilder = field(default_factory=lambda: CandleBuilder(1))
    c5: CandleBuilder = field(default_factory=lambda: CandleBuilder(5))
    ltp: float = 0.0
    prev_close: float = 0.0
    day_open: float = 0.0
    last_tick: datetime | None = None
    expiry: date | None = None
    expiry_flags: list[str] = field(default_factory=list)
    expiry_today: bool = False
    chain_center: float | None = None
    chain_strikes: list[float] = field(default_factory=list)
    orng: OpeningRange | None = None
    phase: str = "before"
    signal: Signal | None = None
    signal_seen_at: datetime | None = None
    plan: dict | None = None
    plan_obj: object | None = None
    risk: dict | None = None
    trade_id: int | None = None
    trade: dict | None = None
    mc: dict | None = None
    mc_running: bool = False
    missed: dict | None = None  # a signal that happened while the app was not running
    yday: dict | None = None    # yesterday's date/open/close/side (trend filter)
    skipped: dict | None = None  # first breakout today that went against yesterday's direction
    versions: dict | None = None  # exit versions A and B of today's paper trade
    trail: dict | None = None     # trailing exit: {"stop": current stop on the index, "active": bool}

    def reset_day(self):
        self.c1.reset()
        self.c5.reset()
        self.day_open = 0.0
        self.orng = None
        self.phase = "before"
        self.signal = None
        self.signal_seen_at = None
        self.plan = self.plan_obj = self.risk = None
        self.trade_id = None
        self.trade = None
        self.mc = None
        self.missed = None
        self.skipped = None
        self.versions = None
        self.trail = None
        self.chain_center = None


def _build() -> str:
    from . import BUILD
    return BUILD


_DISK = {"at": None, "build": None}


def _build_on_disk() -> str | None:
    """The build of the files installed on disk (re-read at most every 30 s). When it differs from the running build,
    a newer version was installed while the app kept running: the dashboard asks for a restart."""
    import re
    import time
    from pathlib import Path
    if _DISK["at"] is None or time.monotonic() - _DISK["at"] > 30:
        _DISK["at"] = time.monotonic()
        try:
            m = re.search(r'BUILD\s*=\s*"([^"]+)"', (Path(__file__).parent / "__init__.py").read_text())
            _DISK["build"] = m.group(1) if m else None
        except OSError:
            _DISK["build"] = None
    return _DISK["build"]


class Engine:
    def __init__(self, settings: Settings, calendar: Calendar, journal: Journal, mode: str, hub: Hub,
                 clock: Callable[[], datetime]):
        self.s = settings
        self.cal = calendar
        self.journal = journal
        self.mode = mode
        self.hub = hub
        self.clock = clock
        self.universes: dict[str, OptionUniverse] = {}
        self.contracts: dict[str, Contract] = {}
        self.quotes: dict[str, Quote] = {}
        self.prev_oi: dict[str, int] = {}
        self.opt_bars: dict[str, CandleBuilder] = {}
        self.idx = {k: IndexState(k, m) for k, m in INDEX_META.items()}
        self.vix = {"ltp": 0.0, "prev_close": 0.0, "last": None}
        self.source_connected = False
        self.source_error = ""
        self.status_note = ""
        self.latencies: deque = deque(maxlen=60)
        self.events: deque = deque(maxlen=300)
        self.day: date | None = None
        self.history: dict[str, dict] = {}
        self.hist_bars: dict[str, list[Bar]] = {}
        self.history_status: dict[str, str] = {k: "Not loaded yet" for k in INDEX_META}
        self.backtests: dict[str, dict] = {}
        self.pools: dict = {}
        self.on_subscriptions: Callable[[dict[int, set[str]]], None] | None = None
        self.on_tick_hook: Callable[[dict, float], None] | None = None
        self._last_subs: dict[int, set[str]] = {}
        self._iv_cache: dict[str, tuple[float, float | None]] = {}
        self._last_feed_state = None
        self.selected_tokens: set[str] = set()
        self.movers = Movers()
        # Candles for every F&O stock, built from the live cash feed; the clicked one is charted.
        self.stock_bars: dict[str, tuple[CandleBuilder, CandleBuilder]] = {}
        self.stock_day: dict[str, date] = {}       # day of history-filled bars (when not today)
        self.chart_stock: str | None = None
        self.ctx1m: dict[str, list[Bar]] = {}            # earlier days, 1-minute (for the chart's context days)
        self._hist_cache: dict[str, tuple[float, list[Bar]]] = {}
        self.stock_daily: dict[str, list[tuple]] = {}   # name -> [(date, o, h, l, c)] daily candles (normal range)
        self.pick_tokens: set[str] = set()              # option quotes for the dashboard's stock picks
        self.on_stock_chart: Callable[[str], None] | None = None   # source hook: fetch today's history
        from .supertrend import SupertrendStrategy
        self.supertrend = SupertrendStrategy(self)                 # second tested strategy (NIFTY, SENSEX)
        from .paper import PaperDesk
        self.paper = PaperDesk(self)                               # your own one-click paper trades

    # ------------------------------------------------------------------ setup

    def log_event(self, kind: str, text: str, level: str = "info") -> None:
        now = self.clock()
        e = {"t": now.isoformat(), "kind": kind, "text": text, "level": level}
        self.events.append(e)
        self.hub.publish("event", e)
        getattr(log, "warning" if level == "warn" else "info")("[%s] %s", kind, text)

    def set_contracts(self, contracts: list[Contract]) -> None:
        self.contracts = {c.token: c for c in contracts}
        self.universes = {k: OptionUniverse(k, contracts) for k in INDEX_META}
        self.day = None  # forces expiry selection on the next evaluate()

    def set_stocks(self, stocks: list[FnoStock], options: list[Contract], label: str) -> None:
        self.movers.load(stocks, options, label)
        self._last_subs = {}
        self._recentre(force=True)

    def select_mover(self, name: str) -> bool:
        if name not in self.movers.stocks:
            return False
        self.movers.select(name, self.clock().date())
        self._recentre(force=True)
        self.chart_stock = name
        if self.on_stock_chart:
            self.on_stock_chart(name)
        return True

    def _stock_builders(self, name: str) -> tuple[CandleBuilder, CandleBuilder]:
        return self.stock_bars.setdefault(name, (CandleBuilder(1), CandleBuilder(5)))

    def backfill_stock(self, name: str, bars1m: list[Bar], day: date) -> None:
        """Fill a stock's chart from Angel's 1-minute history for `day`, keeping any newer live bars."""
        c1, c5 = self._stock_builders(name)
        now = self.clock()
        hist = sorted((b for b in bars1m if b.start.astimezone(IST).date() == day
                       and b.start + timedelta(minutes=1) <= now), key=lambda b: b.start)
        if not hist:
            return
        last = hist[-1].start
        live = [b for b in c1.bars if b.start > last and b.start.astimezone(IST).date() == day]
        ones = [Bar(b.start, b.o, b.h, b.l, b.c, b.v, True, 1) for b in hist] + live
        fives: list[Bar] = []
        for b in ones:
            bucket = c5.bucket(b.start)
            if bucket is None:
                continue
            if fives and fives[-1].start == bucket:
                f = fives[-1]
                f.h, f.l, f.c, f.v = max(f.h, b.h), min(f.l, b.l), b.c, f.v + b.v
            else:
                fives.append(Bar(bucket, b.o, b.h, b.l, b.c, b.v, False, 5))
        for f in fives:
            f.closed = f.start + timedelta(minutes=5) <= now
        c1.bars, c5.bars = ones, fives
        if day != now.date():
            self.stock_day[name] = day
        else:
            self.stock_day.pop(name, None)
        self.hub.publish("stock_ready", {"name": name})

    def stock_json(self, name: str, tf: int) -> dict | None:
        st = self.movers.stocks.get(name)
        if st is None:
            return None
        c1, c5 = self.stock_bars.get(name, (None, None))
        cb = c1 if tf == 1 else c5
        bars = cb.bars if cb else []
        day = self.stock_day.get(name) or (bars[-1].start.astimezone(IST).date() if bars else self.clock().date())
        bars = [b for b in bars if b.start.astimezone(IST).date() == day]          # one session, never two mixed
        first = [b for b in (c1.bars if c1 else []) if ist(day, hm("09:15")) <= b.start < ist(day, hm("09:30"))]
        orng = {"high": max(b.h for b in first), "low": min(b.l for b in first),
                "complete": bool(c1 and any(b.start >= ist(day, hm("09:30")) for b in c1.bars))} if first else None
        return {"name": name, "ltp": round(st.ltp, 2), "prev_close": round(st.prev_close, 2) or None,
                "pct": round(st.pct, 2) if st.pct is not None else None, "day": day.isoformat(),
                "today": day == self.clock().date(), "or": orng, "lot": st.info.lot,
                "bars": [b.to_json() for b in bars]}

    def seed_index(self, key: str, ltp: float, prev_close: float) -> None:
        st = self.idx.get(key)
        if st:
            st.ltp = ltp or st.ltp
            st.prev_close = prev_close or st.prev_close

    def seed_vix(self, ltp: float, prev_close: float) -> None:
        self.vix["ltp"], self.vix["prev_close"] = ltp, prev_close

    def set_history(self, key: str, bars5: list[Bar], vix5: list[Bar] | None, label: str) -> None:
        today = self.clock().date()
        sessions = {d: b for d, b in odds.full_sessions(bars5).items() if d < today}
        vix_s: dict[date, list[Bar]] = {}
        for b in vix5 or []:
            vix_s.setdefault(b.start.astimezone(IST).date(), []).append(b)
        st = self.idx[key]
        u = self.universes.get(key)
        exp = st.expiry or (u.expiries[0] if u and u.expiries else None)
        lot = u.contracts[0].lot if u and u.contracts else int(INDEX_META[key]["lot"])
        step = u.step(exp) if u and exp else float(INDEX_META[key]["step"])
        ctx = odds.Ctx(key, lot, step, st.meta["txn"])
        self.hist_bars[key] = bars5
        older = bars5 + [b for b in st.c5.bars if b.start.astimezone(IST).date() < today]
        st.yday = self._yesterday(older, today)
        bt = odds.backtest(sessions, self.s, ctx, vix_s, prev=odds.yesterday_sides(bars5))
        bt["summary"]["source"] = label
        self.backtests[key] = bt
        self.pools[key] = odds.build_pool(sessions)
        self.history[key] = {"sessions": len(sessions), "label": label}
        self.history_status[key] = (f"{len(sessions)} full sessions" +
                                    (f", {min(sessions)} to {max(sessions)}" if sessions else "") + f" ({label})")
        self.hub.publish("odds", {"index": key})

    # -------------------------------------------------------------- ticks

    def on_tick(self, t: dict, rx: float) -> None:
        now = self.clock()
        if self.on_tick_hook:
            self.on_tick_hook(t, rx)
        ts_ms = t.get("ts") or 0
        ts = datetime.fromtimestamp(ts_ms / 1000.0, IST) if ts_ms > 1e11 else now
        if abs((ts - now).total_seconds()) > 6 * 3600:
            ts = now  # exchange time missing or wildly off: use our clock
        key = (t.get("ws_type"), t.get("token"))
        for st in self.idx.values():
            if key == (st.meta["ws_type"], st.meta["token"]):
                self._index_tick(st, t, ts, now, rx, ts_ms)
                return
        if key == (VIX_META["ws_type"], VIX_META["token"]):
            self.vix["ltp"] = t["ltp"]
            if t.get("close"):
                self.vix["prev_close"] = t["close"]
            self.vix["last"] = now
            return
        if self.movers.on_tick(t, now, self.prev_oi):
            if t.get("ws_type") == 1 and t.get("ltp"):
                name = self.movers.by_eq.get(t.get("token"))
                if name:
                    vol = float(t["vol"]) if t.get("vol") is not None else None
                    builders = self._stock_builders(name)
                    c1 = builders[0]
                    if c1.bars and c1.bars[-1].start.astimezone(IST).date() < ts.astimezone(IST).date():
                        # the chart was showing the last session (opened before today's first trade):
                        # today's first tick starts a fresh chart instead of adding to that day
                        for cb in builders:
                            cb.reset()
                        self.stock_day.pop(name, None)
                        self.hub.publish("stock_ready", {"name": name})
                    for cb in builders:
                        cb.add(t["ltp"], ts, vol)
            return
        c = self.contracts.get(t.get("token")) or self.movers.option_contracts.get(t.get("token"))
        if c is None:
            return
        q = self.quotes.setdefault(c.token, Quote())
        if t.get("ltp"):
            q.ltp = t["ltp"]
        if t.get("close"):
            q.prev_close = t["close"]
        if "oi" in t:
            q.oi = int(t["oi"] or 0)
            q.oi_chg_pct = t.get("oi_chg_pct")
        if c.token in self.prev_oi:
            q.prev_oi = self.prev_oi[c.token]
        if "vol" in t:
            q.volume = float(t["vol"] or 0)
        bids, asks = t.get("bids") or [], t.get("asks") or []
        if bids or asks:
            q.bid, q.bid_qty = (bids[0][0], int(bids[0][1])) if bids else (0.0, 0)
            q.ask, q.ask_qty = (asks[0][0], int(asks[0][1])) if asks else (0.0, 0)
        q.ts = rx
        if q.ltp > 0:
            cb = self.opt_bars.setdefault(c.token, CandleBuilder(1))
            cb.add(q.ltp, ts, q.volume if "vol" in t else None)

    def _index_tick(self, st: IndexState, t: dict, ts: datetime, now: datetime, rx: float, ts_ms: int):
        price = t.get("ltp") or 0.0
        if price <= 0:
            return
        st.ltp = price
        if t.get("close"):
            st.prev_close = t["close"]
        if t.get("open"):
            st.day_open = t["open"]
        st.last_tick = now
        if self.mode == "live" and ts_ms > 1e11:
            self.latencies.append(rx * 1000.0 - ts_ms)
        t_ist = ts.astimezone(IST)
        if t_ist.date() >= AUCTION_FROM and t_ist.time() >= INDEX_AUCTION:
            return    # closing auction: indicative values, not trades - the price updates, the candles stop at 15:15
        st.c1.add(price, ts)
        st.c5.add(price, ts)

    # ----------------------------------------------------------- feed health

    def feed_status(self, now: datetime) -> dict:
        phase = self.cal.phase(now)
        last = max((st.last_tick for st in self.idx.values() if st.last_tick), default=None)
        age = (now - last).total_seconds() if last else None
        lat = statistics.median(self.latencies) if self.latencies else None
        if not self.source_connected:
            state, reason = "disconnected", self.source_error or "not connected to the market-data feed"
        elif phase != "open":
            state, reason = "closed", "market closed"
        elif now.astimezone(IST).time() >= INDEX_AUCTION and (age is None or age > self.s.stale_after_s):
            state, reason = "closed", "closing auction: NIFTY/SENSEX pause from 15:15 (options trade to 15:40)"
        elif age is None:
            state, reason = "waiting", "connected, waiting for the first prices"
        elif age <= self.s.stale_after_s:
            state, reason = "live", ""
        elif age <= self.s.disconnected_after_s:
            state, reason = "delayed", f"no new prices for {age:.0f} s"
        else:
            state, reason = "disconnected", f"no new prices for {age:.0f} s"
        if state != self._last_feed_state and self._last_feed_state is not None:
            lvl = "warn" if state in ("delayed", "disconnected") and phase == "open" else "info"
            self.log_event("feed", f"Feed {state}" + (f": {reason}" if reason else ""), lvl)
        self._last_feed_state = state
        return {"state": state, "reason": reason, "age_s": round(age, 1) if age is not None else None,
                "last": last.isoformat() if last else None,
                "latency_ms": round(lat) if lat is not None else None, "phase": phase,
                "live": state == "live"}

    # -------------------------------------------------------------- evaluate

    @staticmethod
    def _yesterday(bars, today: date) -> dict | None:
        """Yesterday's direction and its range vs the 14-day average (both used by the tested rule)."""
        y = odds.yesterday_for(bars, today)
        if y is not None:
            y["range_atr"] = odds.yesterday_range_atr(bars, today)
        return y

    def _new_day(self, today: date) -> None:
        self.day = today
        for st in self.idx.values():
            built = [b for b in st.c5.bars if b.closed]
            st.reset_day()
            st.yday = self._yesterday(self.hist_bars.get(st.key, []) + built, today)
            u = self.universes.get(st.key)
            if u:
                st.expiry, st.expiry_flags = u.choose_expiry(today)
                st.expiry_today = u.is_expiry_day(today)
                if st.expiry_flags:
                    self.log_event("expiry", st.expiry_flags[0])
            t = self.journal.find_open(today, st.key, self.mode)
            if t:  # app restarted mid-day: pick the day's signal back up
                st.trade_id = t["id"]
        self.opt_bars.clear()
        self.stock_bars.clear()
        self.stock_day.clear()
        self._recentre(force=True)

    def evaluate(self) -> None:
        now = self.clock()
        if self.day != now.date():
            self._new_day(now.date())
        feed = self.feed_status(now)
        for st in self.idx.values():
            st.c1.close_due(now)
            st.c5.close_due(now)
            self._step_index(st, now, feed)
        self.supertrend.step(now, feed)
        self.paper.step(now, feed)
        for cb in self.opt_bars.values():
            cb.close_due(now)
        for c1, c5 in self.stock_bars.values():
            c1.close_due(now)
            c5.close_due(now)
        self._recentre()

    def _step_index(self, st: IndexState, now: datetime, feed: dict) -> None:
        d = now.date()
        if not self.cal.is_trading_day(d):
            st.phase = "holiday"
            return
        if now < ist(d, hm(self.s.or_start)):
            st.phase = "before"
            return
        st.orng = opening_range(st.c5.bars, d, self.s, now)
        if st.signal is None:
            if st.orng is None or not st.orng.complete:
                st.phase = "forming" if now < ist(d, hm("15:30")) else "closed"
                return
            if self.wide_day_skip(st):
                st.phase = "skipday"
                return
            allowed = self.allowed_sides(st)
            state, sig = find_signal(st.key, st.c5.bars, st.orng, d, self.s, now, allowed)
            if allowed is not None and st.skipped is None:
                raw_state, raw = find_signal(st.key, st.c5.bars, st.orng, d, self.s, now)
                if raw_state == "signal" and raw.side not in allowed:
                    st.skipped = {"side": raw.side, "time": raw.time.isoformat(), "close": raw.close}
                    self.log_event("signal", f"{st.meta['label']}: {raw.side} breakout at {fmt_both(raw.time)} skipped: "
                                             f"against yesterday's direction ({next(iter(allowed))} only today).")
            if state == "signal":
                late = (now - sig.time).total_seconds() > self.s.max_signal_age_min * 60
                if late and not st.trade_id:
                    # Found in today's history after the fact (the app was off): no plan from
                    # today's later prices, no paper trade. Show what happened on the index instead.
                    st.signal, st.signal_seen_at = sig, now
                    st.missed = {}
                    self.log_event("signal", f"{st.meta['label']}: today's signal at {fmt_both(sig.time)} happened "
                                             "while the app was off. Shown as missed: no plan, no paper trade.")
                else:
                    self._on_signal(st, sig, now)
            else:
                st.phase = "waiting" if state == "waiting" else "expired"
                return
        if st.missed is not None:
            st.missed = self._missed_info(st, now)
            st.phase = "missed"
            return
        if st.signal and st.plan is None:
            self._try_plan(st, now, feed)
        if st.plan is not None:
            st.phase = "signal"
            self._refresh_risk(st, now, feed)
            self._monitor_trade(st, now, feed)

    def wide_day_skip(self, st: IndexState) -> bool:
        """Tested model: no signal the day after a wide-range day (yesterday's range > wide_day_atr x average)."""
        ra = (st.yday or {}).get("range_atr")
        return bool(self.s.skip_after_wide_day and ra is not None and ra > self.s.wide_day_atr)

    def allowed_sides(self, st: IndexState) -> set[str] | None:
        """Sides the trend filter allows today; None = both (filter off or yesterday unknown)."""
        if self.s.trend_filter != "yesterday" or not st.yday or not st.yday.get("side"):
            return None
        return {st.yday["side"]}

    def trend_text(self, st: IndexState) -> str:
        if self.s.trend_filter != "yesterday":
            return "Trend filter off: both CE and PE allowed."
        y = st.yday
        if not y:
            return "Yesterday's direction is not known yet (history not loaded): both sides allowed for now."
        chg = (y["close"] - y["open"]) / y["open"] * 100 if y["open"] else 0.0
        if not y["side"]:
            return f"Yesterday ({fmt_day(y['date'])}) closed flat: both sides allowed."
        word = "up" if y["side"] == "CE" else "down"
        return (f"{y['side']} only today: yesterday ({y['date']:%a %d %b}) {st.meta['label']} closed {word} "
                f"{chg:+.2f}% from its open ({y['open']:,.2f} to {y['close']:,.2f}).")

    def _missed_info(self, st: IndexState, now: datetime) -> dict:
        sig = st.signal
        stop, targets, risk = index_levels(sig, st.orng, self.s)
        tkey = f"{self.s.target_r:g}R"
        tgt = targets[tkey]
        texit = ist(sig.time.date(), hm(self.s.time_exit))
        use_target = self.s.exit_mode not in ("hold", "trail")
        outcome, exit_px, exit_t, last = "open", None, None, sig.close
        for b in sorted((b for b in st.c5.bars if b.closed and b.start > sig.bar_start), key=lambda b: b.start):
            if b.start + FIVE > texit:
                break
            last = b.c
            if sig.side == "CE":
                if b.l <= stop:
                    outcome, exit_px, exit_t = "stop", min(stop, b.o), b.start + FIVE
                    break
                if use_target and b.h >= tgt:
                    outcome, exit_px, exit_t = "target", tgt, b.start + FIVE
                    break
            else:
                if b.h >= stop:
                    outcome, exit_px, exit_t = "stop", max(stop, b.o), b.start + FIVE
                    break
                if use_target and b.l <= tgt:
                    outcome, exit_px, exit_t = "target", tgt, b.start + FIVE
                    break
        if outcome == "open" and now >= texit:
            outcome, exit_px, exit_t = "time", last, texit
        sign = 1 if sig.side == "CE" else -1
        return {"side": sig.side, "signal_time": sig.time.isoformat(), "bar_start": sig.bar_start.isoformat(),
                "entry_index": round(sig.close, 2), "stop_index": stop, "targets": targets, "target_key": tkey,
                "target_index": tgt, "risk_pts": risk, "outcome": outcome, "exit_mode": self.s.exit_mode,
                "exit_time": exit_t.isoformat() if exit_t else None,
                "exit_index": round(exit_px, 2) if exit_px is not None else None,
                "r_index": round(sign * (exit_px - sig.close) / risk, 2) if exit_px is not None and risk else None}

    def _on_signal(self, st: IndexState, sig: Signal, now: datetime) -> None:
        st.signal = sig
        st.signal_seen_at = now
        word = "above" if sig.side == "CE" else "below"
        lvl = sig.or_high if sig.side == "CE" else sig.or_low
        self.log_event("signal", f"{st.meta['label']}: the {sig.bar_start.strftime('%H:%M')} bar closed at "
                                 f"{sig.close:,.2f}, {word} the opening range ({lvl:,.2f}) -> BUY {sig.side}")
        self._recentre(force=True)

    def _try_plan(self, st: IndexState, now: datetime, feed: dict) -> None:
        u = self.universes.get(st.key)
        sig = st.signal
        if not u or st.expiry is None:
            if st.signal_seen_at and now - st.signal_seen_at > timedelta(seconds=5):
                st.plan = {"error": "No option contracts loaded, so no plan can be made."}
            return
        saved = self.journal.get(st.trade_id) if st.trade_id else None
        if saved and saved.get("token") in self.contracts:
            c = self.contracts[saved["token"]]  # app restarted: keep the contract already logged
        else:
            strike = u.pick_strike(sig.close, st.expiry, sig.side, self.s.strike_mode)
            c = u.contract(st.expiry, strike, sig.side)
        if c is None:
            st.plan = {"error": f"Could not find the {sig.side} contract for this signal in the instrument file."}
            return
        if c.token not in self.selected_tokens:
            self.selected_tokens.add(c.token)
            self._recentre(force=True)
        q = self.quotes.get(c.token, Quote())
        waited = (now - (st.signal_seen_at or now)).total_seconds()
        if q.ltp <= 0 and waited < 10:
            return  # give the option's first quote a few seconds to arrive
        spot = st.ltp or sig.close
        rows, best = compare_strikes(sig, st.orng, u, st.expiry, self.quotes, spot, now, self.s,
                                     st.meta["txn"], st.expiry_flags)
        if self.s.strike_mode == "best" and best and not (saved and saved.get("token") in self.contracts):
            c = self.contracts.get(best["token"], c)
            q = self.quotes.get(c.token, Quote())
        plan = make_plan(sig, st.orng, c, q, spot, now, self.s, st.meta["txn"], st.expiry_flags)
        plan.strikes, plan.best = rows, best
        st.plan_obj = plan
        pj = plan.to_json()
        pj.update(plan_text(pj))
        if saved and saved.get("plan_json"):
            pj = json.loads(saved["plan_json"])  # show the plan exactly as it was logged
        st.plan = pj
        self._refresh_risk(st, now, feed)
        if st.trade_id is None:
            reasons = st.risk["blocks"] if st.risk else []
            st.trade_id = self.journal.open_trade(day=now.date(), plan=pj, verdict=st.risk["verdict"],
                                                  reasons=reasons, mode=self.mode)
        verdict = st.risk["verdict"]
        alert = {"index": st.key, "headline": pj["headline"], "verdict": verdict,
                 "text": f"{pj['headline']} · Stop: {pj['stop_text']} · Target: {pj['target_text']} · "
                         f"{pj['size_text']} · Risk check {verdict}"}
        self.hub.publish("alert", alert)
        self.log_event("plan", alert["text"], "info" if verdict == "PASS" else "warn")
        self._start_mc(st)

    def _ctx(self, st: IndexState, now: datetime, feed: dict, quote: Quote) -> riskmod.RiskContext:
        today = self.journal.today(now.date(), self.mode)
        own_open = 0.0
        if st.trade and st.trade.get("status") == "open" and st.trade.get("kind") == "paper":
            own_open = st.trade.get("planned_loss") or 0.0
        return riskmod.RiskContext(
            now=now, feed_live=feed["live"], feed_reason=feed["reason"] or feed["state"],
            market_open=feed["phase"] == "open", trades_today=today["trades"] - (1 if own_open else 0),
            realized_today=today["realized"], open_risk=today["open_risk"] - own_open,
            spot_now=st.ltp, quote=quote, index_expiry_today=st.expiry_today, demo=self.mode != "live")

    def _refresh_risk(self, st: IndexState, now: datetime, feed: dict) -> None:
        if st.plan_obj is None:
            return
        tr = self.journal.get(st.trade_id) if st.trade_id else None
        st.trade = tr
        if tr and (tr["status"] == "closed" or tr["kind"] == "paper") and st.risk:
            return  # the verdict at entry stands once the paper trade is on (or done)
        q = self.quotes.get(st.plan_obj.contract.token, Quote())
        st.risk = riskmod.check(st.plan_obj, self._ctx(st, now, feed, q), self.s).to_json()
        if tr and tr["kind"] == "shadow" and tr["status"] == "open" and st.risk["verdict"] == "PASS":
            tick = st.plan_obj.contract.tick
            fill = (q.ask + slip(tick, self.s)) if q.ask > 0 else st.plan_obj.entry_fill
            self.journal.promote(tr["id"], round(fill, 2))
            st.trade = self.journal.get(tr["id"])
            self.log_event("plan", f"{st.meta['label']}: the signal now passes the risk check "
                                   f"(it was blocked at first). Paper entry ≈ ₹{fill:,.2f}.")

    def _monitor_trade(self, st: IndexState, now: datetime, feed: dict) -> None:
        if not st.trade_id:
            return
        tr = st.trade or self.journal.get(st.trade_id)
        if not tr or tr["status"] != "open":
            return
        p = st.plan
        q = self.quotes.get(p["contract"]["token"], Quote())
        exit_t = datetime.fromisoformat(p["exit_time"])
        if p.get("exit_mode") in ("hold", "half"):
            self._monitor_versions(st, tr, p, q, now, feed, exit_t)
            return
        stop_now, trailing = p["stop_index"], False
        if p.get("exit_mode") == "trail":
            stop_now, trailing = trail_stop(p, st.c5.bars)
            st.trail = {"stop": round(stop_now, 2), "active": trailing}
        reason = None
        if now >= exit_t:
            reason = "time"
        elif feed["live"]:
            sp = st.ltp
            use_target = p.get("exit_mode", "target") not in ("hold", "trail")
            hit = "trail stop" if trailing else "stop"
            if p["side"] == "CE":
                reason = hit if sp <= stop_now else (
                    "target" if use_target and sp >= p["target_index"] else None)
            else:
                reason = hit if sp >= stop_now else (
                    "target" if use_target and sp <= p["target_index"] else None)
        if reason is None:
            st.trade = dict(tr, live_pnl=self._mark(tr, q))
            return
        if reason == "time" and not feed["live"] and q.ltp <= 0:
            return
        tick = st.plan_obj.contract.tick if st.plan_obj else 0.05
        exit_fill = (q.bid - slip(tick, self.s)) if q.bid > 0 else q.ltp * (1 - self.s.slippage_pct_no_quote / 100)
        exit_fill = max(exit_fill, 0.05)
        c = round_trip(tr["entry_fill"], exit_fill, tr["qty"], st.meta["txn"], self.s)
        closed = self.journal.close_trade(st.trade_id, exit_time=now, exit_index=st.ltp, exit_fill=round(exit_fill, 2),
                                          reason=reason, costs=c.total)
        st.trade = closed
        what = {"time": "time exit at 15:00", "stop": "stop hit", "trail stop": "trailing stop hit",
                "target": "target hit"}[reason]
        kind = "Paper trade" if closed.get("kind") == "paper" else "Skipped (blocked) signal"
        self.log_event("exit", f"{kind} {p['contract']['label']}: {what}. Exit ≈ ₹{exit_fill:,.2f}, "
                               f"P&L after costs ₹{closed.get('pnl', 0):,.0f}.")
        self.hub.publish("alert", {"index": st.key, "headline": f"{st.meta['label']}: {what}",
                                   "text": f"{kind}: P&L after costs ₹{closed.get('pnl', 0):,.0f}",
                                   "verdict": "EXIT"})

    def _exit_fill(self, st: IndexState, q: Quote) -> float | None:
        if q.bid <= 0 and q.ltp <= 0:
            return None
        tick = st.plan_obj.contract.tick if st.plan_obj else 0.05
        px = (q.bid - slip(tick, self.s)) if q.bid > 0 else q.ltp * (1 - self.s.slippage_pct_no_quote / 100)
        return round(max(px, 0.05), 2)

    def _version_pnl(self, tr: dict, ver: dict, exch: str, fill_now: float | None = None) -> tuple[float, float]:
        """(pnl after costs, costs) of a version; open parts are marked at fill_now."""
        qty, entry = tr["qty"], tr["entry_fill"]
        half = ver.get("half")
        rest = ver.get("exit", {}).get("fill") if ver.get("exit") else fill_now
        if rest is None:
            return 0.0, 0.0
        if not half:
            c = round_trip(entry, rest, qty, exch, self.s)
            return qty * (rest - entry) - c.total, c.total
        h = qty / 2
        c = round_trip(entry, half["fill"], h, exch, self.s).total + round_trip(entry, rest, h, exch, self.s, orders=1).total
        return h * (half["fill"] - entry) + h * (rest - entry) - c, c

    def _monitor_versions(self, st: IndexState, tr: dict, p: dict, q: Quote, now: datetime, feed: dict,
                          exit_t: datetime) -> None:
        """Track exit versions A (hold to the stop or 15:00) and B (sell half at +1R, then the rest's stop
        moves to entry) on the same paper trade. The row closes when A closes (A always closes last)."""
        exch = st.meta["txn"]
        v = st.versions or json.loads(tr.get("versions_json") or "null") or {
            "A": {"open": True}, "B": {"open": True, "half": None, "stop": p["stop_index"]}}
        ce = p["side"] == "CE"
        sp = st.ltp
        risk = abs(p["entry_index"] - p["stop_index"])
        half_lvl = p["entry_index"] + (self.s.half_at_r * risk if ce else -self.s.half_at_r * risk)
        crossed = (lambda lvl: sp <= lvl) if ce else (lambda lvl: sp >= lvl)   # back through a stop
        reached = (lambda lvl: sp >= lvl) if ce else (lambda lvl: sp <= lvl)   # a profit level
        timeup = now >= exit_t
        fill = self._exit_fill(st, q)
        changed = False
        A, B = v["A"], v["B"]
        label = st.meta["label"]
        if fill is not None:
            if B["open"] and feed["live"] and not timeup:
                if B["half"] is None and reached(half_lvl):
                    B["half"] = {"fill": fill, "time": now.isoformat(), "index": round(sp, 2)}
                    B["stop"] = p["entry_index"]
                    changed = True
                    self.log_event("exit", f"{label} version B: +{self.s.half_at_r:g}R reached, half sold ≈ ₹{fill:,.2f}; "
                                           f"the rest's stop moves to entry {p['entry_index']:,.2f}.")
                    self.hub.publish("alert", {"index": st.key, "headline": f"{label}: version B, sell half now",
                                               "text": f"+{self.s.half_at_r:g}R reached. Move the stop on the rest "
                                                       f"to {p['entry_index']:,.2f}.", "verdict": "EXIT"})
                elif B["half"] is not None and crossed(B["stop"]):
                    B.update(open=False, exit={"fill": fill, "time": now.isoformat(), "index": round(sp, 2),
                                               "reason": "stop at entry"})
                    changed = True
            if A["open"] and (timeup or (feed["live"] and crossed(p["stop_index"]))):
                reason = "time" if timeup else "stop"
                A.update(open=False, exit={"fill": fill, "time": now.isoformat(), "index": round(sp, 2), "reason": reason})
                if B["open"]:
                    B.update(open=False, exit={"fill": fill, "time": now.isoformat(), "index": round(sp, 2),
                                               "reason": reason})
                changed = True
        for key in ("A", "B"):
            ver = v[key]
            if not ver["open"] and ver.get("pnl") is None:
                pnl, costs = self._version_pnl(tr, ver, exch)
                ver.update(pnl=round(pnl, 2), costs=round(costs, 2),
                           r=round(pnl / tr["planned_loss"], 3) if tr["planned_loss"] else 0.0)
        st.versions = v
        if changed:
            self.journal.set_versions(st.trade_id, v)
        if A["open"]:
            live = {k: round(self._version_pnl(tr, v[k], exch, fill)[0], 2) if v[k]["open"] else v[k].get("pnl")
                    for k in ("A", "B")} if fill is not None else None
            st.trade = dict(tr, live_pnl=self._mark(tr, q), versions=v, live_versions=live)
            return
        main = v["B"] if p.get("exit_mode") == "half" else v["A"]
        ex = main["exit"]
        if main.get("half"):
            avg = (main["half"]["fill"] + ex["fill"]) / 2
            why = "half at +1R, rest " + ex["reason"]
        else:
            avg, why = ex["fill"], ex["reason"]
        closed = self.journal.close_trade(st.trade_id, exit_time=now, exit_index=ex["index"], exit_fill=round(avg, 2),
                                          reason=why, costs=main["costs"])
        closed["versions"] = v
        st.trade = closed
        kind = "Paper trade" if closed.get("kind") == "paper" else "Skipped (blocked) signal"
        what = "time exit at 15:00" if ex["reason"] == "time" else "stop hit"
        self.log_event("exit", f"{kind} {p['contract']['label']}: {what}. Version A ₹{v['A']['pnl']:,.0f}, "
                               f"version B ₹{v['B']['pnl']:,.0f} after costs.")
        self.hub.publish("alert", {"index": st.key, "headline": f"{label}: {what}",
                                   "text": f"{kind}: A ₹{v['A']['pnl']:,.0f} · B ₹{v['B']['pnl']:,.0f} after costs",
                                   "verdict": "EXIT"})

    def settle_from_candles(self, tr: dict, idx1m: list[Bar], opt1m: list[Bar]) -> dict | None:
        """A paper trade still open from an earlier day (the app was closed before its exit): work out the exit
        it would have made from that day's 1-minute candles of the index and the option - the same rules as live
        (stop on the index, version B's half at +1R then stop at entry, time exit at 15:00). The stop is checked
        before the +1R level inside a candle (the cautious reading). Returns the closed row, or None."""
        p = json.loads(tr["plan_json"])
        ce = p["side"] == "CE"
        t_in = datetime.fromisoformat(p["signal_time"])
        exit_t = datetime.fromisoformat(p["exit_time"])
        bars = sorted((b for b in idx1m if t_in <= b.start < exit_t), key=lambda b: b.start)
        opt = {b.start: b for b in opt1m}
        if not bars or not opt:
            return None

        def fill_at(t: datetime) -> float | None:
            ks = [k for k in opt if k <= t]
            if not ks:
                return None
            return round(max(opt[max(ks)].c * (1 - self.s.slippage_pct_no_quote / 100), 0.05), 2)

        entry, stop = p["entry_index"], p["stop_index"]
        risk = abs(entry - stop)
        half_lvl = entry + (self.s.half_at_r * risk if ce else -self.s.half_at_r * risk)
        hit = (lambda b, lvl: b.l <= lvl) if ce else (lambda b, lvl: b.h >= lvl)        # back through a stop
        reach = (lambda b, lvl: b.h >= lvl) if ce else (lambda b, lvl: b.l <= lvl)      # a profit level
        A = {"open": True}
        B = {"open": True, "half": None, "stop": stop}
        for b in bars:
            t = b.start + timedelta(minutes=1)
            if hit(b, stop):
                ex = {"fill": fill_at(b.start), "time": t.isoformat(), "index": round(stop, 2), "reason": "stop"}
                A.update(open=False, exit=ex)
                if B["open"]:
                    B.update(open=False, exit=dict(ex))
                break
            if B["open"] and B["half"] is not None and hit(b, B["stop"]):
                B.update(open=False, exit={"fill": fill_at(b.start), "time": t.isoformat(), "index": round(entry, 2),
                                           "reason": "stop at entry"})
            elif B["open"] and B["half"] is None and reach(b, half_lvl):
                B["half"] = {"fill": fill_at(b.start), "time": t.isoformat(), "index": round(half_lvl, 2)}
                B["stop"] = entry
        if A["open"]:
            last = bars[-1]
            ex = {"fill": fill_at(exit_t), "time": exit_t.isoformat(), "index": round(last.c, 2), "reason": "time"}
            A.update(open=False, exit=ex)
            if B["open"]:
                B.update(open=False, exit=dict(ex))
        if any(x["exit"]["fill"] is None for x in (A, B)) or (B["half"] and B["half"]["fill"] is None):
            return None
        exch = INDEX_META[tr["idx"]]["txn"]
        v = {"A": A, "B": B}
        for ver in v.values():
            pnl, costs = self._version_pnl(tr, ver, exch)
            ver.update(pnl=round(pnl, 2), costs=round(costs, 2),
                       r=round(pnl / tr["planned_loss"], 3) if tr["planned_loss"] else 0.0, settled_later=True)
        self.journal.set_versions(tr["id"], v)
        main = B if p.get("exit_mode") == "half" else A
        ex = main["exit"]
        avg = (main["half"]["fill"] + ex["fill"]) / 2 if main.get("half") else ex["fill"]
        why = ("half at +1R, rest " + ex["reason"] if main.get("half") else ex["reason"]) + " (worked out later: app was off)"
        closed = self.journal.close_trade(tr["id"], exit_time=datetime.fromisoformat(ex["time"]), exit_index=ex["index"],
                                          exit_fill=round(avg, 2), reason=why, costs=main["costs"])
        self.log_event("exit", f"{p['contract']['label']} ({tr['day']}): the app was off at the exit, so it was worked "
                               f"out from that day's 1-minute candles: {ex['reason']}, P&L after costs "
                               f"₹{closed.get('pnl', 0):,.0f}.")
        return closed

    def _mark(self, tr: dict, q: Quote) -> dict | None:
        if q.ltp <= 0:
            return None
        px = q.bid if q.bid > 0 else q.ltp
        gross = tr["qty"] * (px - tr["entry_fill"])
        c = round_trip(tr["entry_fill"], px, tr["qty"], INDEX_META[tr["idx"]]["txn"], self.s)
        return {"exit_now": round(px, 2), "pnl": round(gross - c.total, 2)}

    def _start_mc(self, st: IndexState) -> None:
        pool = self.pools.get(st.key)
        if pool is None or st.plan_obj is None or st.mc_running:
            if pool is None:
                st.mc = {"error": "History is still loading; the replay will run once it is ready."}
            return
        p = st.plan_obj
        st.mc_running = True
        option = {"strike": p.contract.strike, "expiry": p.contract.expiry.isoformat(), "iv": p.iv,
                  "entry_fill": p.entry_fill, "lot": p.contract.lot, "lots": p.sizing.lots,
                  "exch": st.meta["txn"], "slip_pct": self.s.slippage_pct_no_quote,
                  "planned_loss": p.sizing.loss_at_stop if p.sizing.lots else p.sizing.one_lot_loss}
        args = (pool, p.side, p.entry_index, p.stop_index, p.target_index, p.signal_time, self.s, option)

        async def run():
            try:
                st.mc = await asyncio.to_thread(odds.monte_carlo, *args)
            except Exception as e:  # pragma: no cover
                log.exception("Monte Carlo failed")
                st.mc = {"error": f"Replay failed: {e}"}
            finally:
                st.mc_running = False
            self.hub.publish("odds", {"index": st.key})

        try:
            asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            st.mc = odds.monte_carlo(*args)
            st.mc_running = False

    def rerun_mc(self, key: str) -> None:
        st = self.idx[key]
        if not st.mc_running:
            self._start_mc(st)

    # ---------------------------------------------------------- option chain

    def _recentre(self, force: bool = False) -> None:
        changed = force
        for st in self.idx.values():
            u = self.universes.get(st.key)
            if not u or st.expiry is None or st.ltp <= 0:
                continue
            step = u.step(st.expiry)
            atm = u.atm(st.ltp, st.expiry)
            if st.chain_center is None or abs(atm - st.chain_center) >= 3 * step:
                st.chain_center = atm
                st.chain_strikes = u.chain(atm, st.expiry, self.s.chain_strikes_each_side)
                changed = True
        if not changed:
            return
        want: dict[int, set[str]] = {}
        for st in self.idx.values():
            want.setdefault(st.meta["ws_type"], set()).add(st.meta["token"])
        want.setdefault(VIX_META["ws_type"], set()).add(VIX_META["token"])
        for st in self.idx.values():
            u = self.universes.get(st.key)
            if not u or st.expiry is None:
                continue
            for k in st.chain_strikes:
                for opt in ("CE", "PE"):
                    c = u.contract(st.expiry, k, opt)
                    if c:
                        want.setdefault(c.ws_type, set()).add(c.token)
        for tok in self.selected_tokens:
            c = self.contracts.get(tok)
            if c:
                want.setdefault(c.ws_type, set()).add(tok)
        for wt, toks in self.movers.tokens().items():
            want.setdefault(wt, set()).update(toks)
        for tok in self.movers.selected_tokens | self.pick_tokens | self.paper.tokens:
            c = self.movers.option_contracts.get(tok)
            if c:
                want.setdefault(c.ws_type, set()).add(tok)
        if want != self._last_subs:
            self._last_subs = {k: set(v) for k, v in want.items()}
            if self.on_subscriptions:
                self.on_subscriptions(want)

    def select_token(self, token: str) -> bool:
        if token in self.contracts:
            self.selected_tokens.add(token)
            self._recentre(force=True)
            return True
        return False

    def _greeks(self, c: Contract, q: Quote, spot: float, now: datetime) -> dict:
        r = self.s.risk_free_rate_pct / 100.0
        T = bs.years_to_expiry(now, c.expiry)
        key = c.token
        px = q.mid if q.mid > 0 else q.ltp
        cached = self._iv_cache.get(key)
        if cached and abs(cached[0] - px) < 1e-9 and cached[2] == round(spot, 1):
            iv = cached[1]
        else:
            iv = bs.implied_vol(px, spot, c.strike, T, r, c.opt) if px > 0 and spot > 0 else None
            self._iv_cache[key] = (px, iv, round(spot, 1))
        if iv is None:
            return {"iv": None, "delta": None, "theta": None}
        return {"iv": round(iv * 100, 1), "delta": round(bs.delta(spot, c.strike, T, r, iv, c.opt), 2),
                "theta": round(bs.theta_per_day(spot, c.strike, T, r, iv, c.opt), 2)}

    def chain_json(self, key: str) -> dict:
        st = self.idx[key]
        u = self.universes.get(key)
        now = self.clock()
        rows = []
        if u and st.expiry:
            atm = u.atm(st.ltp, st.expiry) if st.ltp else None
            for k in st.chain_strikes:
                row = {"strike": k, "atm": k == atm}
                for opt in ("CE", "PE"):
                    c = u.contract(st.expiry, k, opt)
                    if not c:
                        row[opt] = None
                        continue
                    q = self.quotes.get(c.token)
                    if not q or q.ltp <= 0:
                        row[opt] = {"token": c.token, "ltp": None}
                        continue
                    chg = q.ltp - q.prev_close if q.prev_close else None
                    row[opt] = {"token": c.token, "ltp": round(q.ltp, 2), "chg": round(chg, 2) if chg is not None else None,
                                "chg_pct": round(chg / q.prev_close * 100, 1) if chg is not None and q.prev_close else None,
                                "oi": q.oi, "oi_chg": q.oi_change, "vol": q.volume,
                                "bid": round(q.bid, 2), "ask": round(q.ask, 2),
                                **self._greeks(c, q, st.ltp, now)}
                rows.append(row)
        plan_token = st.plan.get("contract", {}).get("token") if st.plan and "contract" in st.plan else None
        return {"index": key, "expiry": st.expiry.isoformat() if st.expiry else None,
                "expiry_label": fmt_day(st.expiry) if st.expiry else None,
                "spot": round(st.ltp, 2), "rows": rows, "plan_token": plan_token,
                "lot": u.contracts[0].lot if u and u.contracts else None,
                "step": u.step(st.expiry) if u and st.expiry else None}

    # ------------------------------------------------------------- snapshots

    def card(self, st: IndexState, now: datetime) -> dict:
        d = now.date()
        label = st.meta["label"]
        o = st.orng.to_json() if st.orng else None
        or_end = ist(d, hm(self.s.or_end))
        first_sig = or_end + timedelta(minutes=5)
        last = ist(d, hm(self.s.last_signal_time))
        if st.phase == "holiday":
            return {"state": "closed", "title": f"Market closed today ({self.cal.holiday_name(d) or 'holiday'})",
                    "text": f"Next session opens {fmt_both(self.cal.next_open(now))} on "
                            f"{self.cal.next_open(now).strftime('%a %d %b')}."}
        if st.phase == "before":
            nxt = ist(d, hm(self.s.or_start))
            return {"state": "before", "title": f"{label}: waiting for the open",
                    "text": f"The opening range forms {fmt_both(nxt)} to {fmt_both(or_end)}. "
                            f"The first possible signal is at {fmt_both(first_sig)}."}
        if st.phase == "forming":
            rng = f"so far high {o['high']:,.2f}, low {o['low']:,.2f}" if o else "no prices yet"
            return {"state": "forming", "title": f"{label}: opening range forming",
                    "text": f"09:15-09:30 IST range {rng}. First possible signal at {fmt_both(first_sig)}."}
        if st.phase == "waiting":
            closed = st.c5.closed_bars()
            lc = closed[-1].c if closed else st.ltp
            buf = breakout_margin(st.orng, self.s) if st.orng else 0.0
            up, dn = o["high"] + buf, o["low"] - buf
            allowed = self.allowed_sides(st) or {"CE", "PE"}
            want = " or ".join(x for x in (f"above {up:,.2f} (BUY CE)" if "CE" in allowed else "",
                                           f"below {dn:,.2f} (BUY PE)" if "PE" in allowed else "") if x)
            skip = ""
            if st.skipped:
                skip = (f" A {st.skipped['side']} breakout at {fmt_both(datetime.fromisoformat(st.skipped['time']))} "
                        "was skipped because it went against yesterday's direction.")
            return {"state": "waiting", "title": f"{label}: waiting for a breakout",
                    "text": f"Waiting for a 5-minute close {want}. Range {o['low']:,.2f} to {o['high']:,.2f}; the "
                            f"close must clear it by {buf:,.2f} points ({self.s.breakout_buffer:g}x the width). "
                            f"Last 5-minute close {lc:,.2f}. {self.trend_text(st)}{skip} "
                            f"No new signals after {fmt_both(last)}."}
        if st.phase == "expired":
            extra = (" A breakout against yesterday's direction was skipped." if st.skipped else "")
            return {"state": "expired", "title": f"{label}: no trade today",
                    "text": f"No qualifying 5-minute close beyond the opening range ({o['low']:,.2f} to {o['high']:,.2f}) "
                            f"by {fmt_both(last)}.{extra} The rule allows one signal a day, so this index is done."}
        if st.phase == "closed":
            return {"state": "closed", "title": f"{label}: session over", "text": "The market is closed."}
        if st.phase == "skipday":
            ra = (st.yday or {}).get("range_atr") or 0.0
            return {"state": "expired", "title": f"{label}: no trade today (after a wide-range day)",
                    "text": f"Yesterday's range was {ra:.2f}x its 14-day average (above {self.s.wide_day_atr:g}x). The "
                            "tested model skips the opening-range breakout the day after a wide-range day: in testing "
                            "those days lost money. Settings → Strategy → skip_after_wide_day turns this off."}
        if st.phase == "missed" and st.missed:
            m = st.missed
            word = "above" if m["side"] == "CE" else "below"
            when = fmt_both(datetime.fromisoformat(m["exit_time"])) if m.get("exit_time") else ""
            if m["outcome"] == "target":
                res = f"the target was hit at {when}"
            elif m["outcome"] == "stop":
                res = f"the stop was hit at {when}"
            elif m["outcome"] == "time":
                res = (f"{'the stop was not' if m.get('exit_mode') in ('hold', 'trail') else 'neither was'} hit; at the "
                       f"{self.s.time_exit} exit the index was at {m['exit_index']:,.2f}")
            else:
                res = ("the stop has not been hit yet" if m.get("exit_mode") in ("hold", "trail")
                       else "neither the stop nor the target has been hit yet")
            r = f" ({m['r_index']:+.2f}R on the index)" if m.get("r_index") is not None else ""
            return {"state": "missed", "title": f"{label}: today's signal came while the app was off",
                    "text": f"BUY {m['side']} at {fmt_both(datetime.fromisoformat(m['signal_time']))}: the 5-minute bar "
                            f"closed at {m['entry_index']:,.2f}, {word} the opening range. On the index: stop "
                            f"{m['stop_index']:,.2f}, " + (f"no fixed target (hold to {self.s.time_exit} IST)"
                            if m.get("exit_mode") in ("hold", "trail") else f"target {m['target_key']} {m['target_index']:,.2f}")
                            + ". Result so far: "
                            f"{res}{r}. There is no premium plan or paper trade, because the option prices from that "
                            "moment aren't available here and a late entry is a different trade."}
        if st.phase == "signal" and st.plan:
            return {"state": "signal", "title": st.plan.get("headline", "Signal"), "text": ""}
        return {"state": st.phase, "title": label, "text": ""}

    def state_json(self) -> dict:
        now = self.clock()
        feed = self.feed_status(now)
        today = self.journal.today(now.date(), self.mode)
        out = {
            "mode": self.mode, "now": now.isoformat(), "build": _build(), "build_on_disk": _build_on_disk(),
            "now_ist": now.astimezone(IST).strftime("%H:%M:%S"), "now_uk": now.astimezone(UK).strftime("%H:%M:%S"),
            "date": now.strftime("%a %d %b %Y"), "feed": feed, "note": self.status_note,
            "vix": {"ltp": round(self.vix["ltp"], 2),
                    "chg": round(self.vix["ltp"] - self.vix["prev_close"], 2) if self.vix["prev_close"] else None},
            "today": {**today, "daily_limit": self.s.daily_loss_limit, "max_trades": self.s.max_trades_per_day,
                      "max_loss": self.s.max_loss_per_trade, "max_loss_confirmed": self.s.max_loss_confirmed},
            "indices": {},
        }
        for k, st in self.idx.items():
            tte = None
            if st.expiry:
                exp_dt = ist(st.expiry, hm("15:30"))
                tte = humanize_delta((exp_dt - now).total_seconds())
            chg = st.ltp - st.prev_close if st.prev_close else None
            out["indices"][k] = {
                "label": st.meta["label"], "ltp": round(st.ltp, 2),
                "chg": round(chg, 2) if chg is not None else None,
                "chg_pct": round(chg / st.prev_close * 100, 2) if chg is not None and st.prev_close else None,
                "phase": st.phase, "or": st.orng.to_json() if st.orng else None,
                "card": self.card(st, now),
                "signal": st.signal.to_json() if st.signal else None,
                "plan": st.plan, "risk": st.risk, "trade": _trade_view(st.trade), "missed": st.missed,
                "expiry": st.expiry.isoformat() if st.expiry else None,
                "expiry_label": fmt_day(st.expiry) if st.expiry else None,
                "expiry_flags": st.expiry_flags, "tte": tte,
                "history": self.history_status.get(k),
            }
        return out

    def bars_json(self, key: str, tf: int) -> list[dict]:
        cb = self.idx[key].c1 if tf == 1 else self.idx[key].c5
        return [b.to_json() for b in cb.bars]

    # ------------------------------------------------- earlier days on the chart (liquidity levels)

    def set_context1m(self, key: str, bars: list[Bar]) -> None:
        self.ctx1m[key] = sorted(bars, key=lambda b: b.start)

    def _hist5(self, key: str) -> list[Bar]:
        from .history import load_cached
        p = DATA_DIR / "history" / f"{key}_5m.json"
        try:
            m = p.stat().st_mtime
        except OSError:
            return []
        c = self._hist_cache.get(key)
        if c and c[0] == m:
            return c[1]
        try:
            bars = load_cached(DATA_DIR / "history", key)
        except (ValueError, OSError):
            bars = []
        self._hist_cache[key] = (m, bars)
        return bars

    @staticmethod
    def _agg5(bars1m: list[Bar]) -> list[Bar]:
        out: list[Bar] = []
        for b in bars1m:
            s = b.start.replace(second=0, microsecond=0)
            s -= timedelta(minutes=s.minute % 5)
            if out and out[-1].start == s:
                o = out[-1]
                o.h, o.l, o.c, o.v = max(o.h, b.h), min(o.l, b.l), b.c, o.v + (b.v or 0)
            else:
                out.append(Bar(s, b.o, b.h, b.l, b.c, b.v or 0, True, 5))
        return out

    # ------------------------------------------------- scrolling back through older days (feature 2)

    def _hist5_days(self, key: str) -> dict[date, list[Bar]]:
        bars = self._hist5(key)
        c = getattr(self, "_h5d", {}).get(key)
        if c and c[0] is bars:
            return c[1]
        by: dict[date, list[Bar]] = {}
        for b in bars:
            by.setdefault(b.start.astimezone(IST).date(), []).append(b)
        if not hasattr(self, "_h5d"):
            self._h5d = {}
        self._h5d[key] = (bars, by)
        return by

    def chart_days(self, key: str) -> list[date]:
        """Every past session we have candles for (5-minute history, 1-minute files, saved context)."""
        s = set(self._hist5_days(key)) | {b.start.astimezone(IST).date() for b in self.ctx1m.get(key, [])}
        try:
            from .localdata import days_1m
            s |= {date.fromisoformat(d) for d in days_1m(key)}
        except Exception:                                   # pragma: no cover
            pass
        today = self.clock().astimezone(IST).date()
        return sorted(d for d in s if d < today)

    def _bars_for_days(self, key: str, tf: int, days: list[date]) -> list[Bar]:
        five = self._hist5_days(key)
        one: dict[date, list[Bar]] = {}
        want = set(days)
        for b in self.ctx1m.get(key, []):
            d = b.start.astimezone(IST).date()
            if d in want:
                one.setdefault(d, []).append(b)
        need = [d.isoformat() for d in days if d not in one and (tf == 1 or d not in five)]
        if need:
            try:
                from .localdata import day_bars_1m
                for iso, rows in day_bars_1m(key, need).items():
                    one[date.fromisoformat(iso)] = [Bar(datetime.fromisoformat(r[0]).astimezone(IST), r[1], r[2], r[3],
                                                        r[4], r[5], True, 1) for r in rows]
            except Exception as ex:                         # pragma: no cover
                log.info("Saved 1-minute files unavailable for %s: %s", key, ex)
        out: list[Bar] = []
        for d in sorted(days):
            if tf == 1:
                out += one.get(d) or five.get(d, [])
            else:
                out += five.get(d) or (self._agg5(one[d]) if d in one else [])
        return out

    def older_json(self, key: str, tf: int, before: date, n: int = 5, around: date | None = None) -> dict:
        """Sessions before `before` (the n latest of them), or, with `around`, a few days either side of that day."""
        if key not in self.idx:
            return {"bars": [], "days": [], "more": False}
        avail = self.chart_days(key)
        if around is not None:
            i = next((k for k, d in enumerate(avail) if d >= around), len(avail))
            pick = avail[max(0, i - 3): i + 6]
        else:
            older = [d for d in avail if d < before]
            pick = older[-max(1, min(int(n), 20)):]
        bars = self._bars_for_days(key, 1 if tf == 1 else 5, pick)
        return {"bars": [b.to_json() for b in bars], "days": [d.isoformat() for d in pick],
                "more": bool(pick) and avail[0] < pick[0]}

    def context_json(self, key: str, tf: int, days: int) -> dict:
        """The `days` sessions before today: candles (1-minute only where saved) and their highs/lows/closes."""
        out = {"bars": [], "levels": [], "days": []}
        days = max(0, min(int(days or 0), 5))
        if not days or key not in self.idx:
            return out
        today = self.clock().astimezone(IST).date()
        dof = lambda b: b.start.astimezone(IST).date()
        one: dict[date, list[Bar]] = {}
        for b in self.ctx1m.get(key, []):
            if dof(b) < today:
                one.setdefault(dof(b), []).append(b)
        five: dict[date, list[Bar]] = {}
        for b in self._hist5(key):
            if dof(b) < today:
                five.setdefault(dof(b), []).append(b)
        avail = sorted(set(one) | set(five))[-days:]
        missing = [d.isoformat() for d in avail if d not in one]
        if missing:                                    # no saved 1-minute candles: try your own 1-minute files
            try:
                from .localdata import day_bars_1m
                for iso, rows in day_bars_1m(key, missing).items():
                    one[date.fromisoformat(iso)] = [Bar(datetime.fromisoformat(r[0]).astimezone(IST), r[1], r[2], r[3],
                                                        r[4], r[5], True, 1) for r in rows]
            except Exception as ex:                    # pragma: no cover - a damaged file must not break the chart
                log.info("Saved 1-minute files unavailable for %s: %s", key, ex)
        bars: list[Bar] = []
        for d in avail:
            if tf == 1:
                bars += one.get(d) or five.get(d, [])   # 5-minute candles if no 1-minute ones exist for that day
            else:
                bars += self._agg5(one[d]) if d in one else five.get(d, [])
        out["bars"] = [b.to_json() for b in bars]
        out["days"] = [d.isoformat() for d in avail]
        # only the previous day's high and low (PDH / PDL), whatever number of days is shown
        src = one.get(avail[-1]) or five.get(avail[-1]) or [] if avail else []
        if src:
            out["levels"] = [{"label": "PDH", "price": round(max(b.h for b in src), 2), "kind": "high"},
                             {"label": "PDL", "price": round(min(b.l for b in src), 2), "kind": "low"}]
        return out

    def latest_bars(self) -> dict:
        out = {}
        for k, st in self.idx.items():
            out[k] = {"ltp": round(st.ltp, 2),
                      "1": [b.to_json() for b in st.c1.bars[-2:]],
                      "5": [b.to_json() for b in st.c5.bars[-2:]]}
        name = self.chart_stock
        if name and name in self.stock_bars and name not in self.stock_day:
            c1, c5 = self.stock_bars[name]
            sst = self.movers.stocks.get(name)
            out["STOCK"] = {"name": name, "ltp": round(sst.ltp, 2) if sst else None,
                            "1": [b.to_json() for b in c1.bars[-2:]], "5": [b.to_json() for b in c5.bars[-2:]]}
        return out

    def latest_opt_bars(self) -> dict:
        return {tok: cb.bars[-1].to_json() for tok, cb in self.opt_bars.items() if cb.bars}

    def option_json(self, token: str) -> dict | None:
        c = self.contracts.get(token)
        if not c:
            return None
        q = self.quotes.get(token, Quote())
        st = self.idx[c.name]
        cb = self.opt_bars.get(token)
        return {"contract": {**c.to_json(), "label": c.label()},
                "quote": {"ltp": q.ltp, "bid": q.bid, "ask": q.ask, "oi": q.oi, "oi_chg": q.oi_change,
                          "vol": q.volume, "prev_close": q.prev_close},
                "greeks": self._greeks(c, q, st.ltp, self.clock()) if q.ltp > 0 else {},
                "bars": [b.to_json() for b in cb.bars] if cb else []}

    def movers_json(self, n: int = 8) -> dict:
        m = self.movers
        detail = None
        if m.selected and m.selected in m.stocks:
            st = m.stocks[m.selected]
            now = self.clock()
            u = m.universes.get(m.selected)
            if st.ltp > 0 and u:
                exp, _ = u.choose_expiry(now.date())
                if not m.selected_tokens or (exp and u.atm(st.ltp, exp) != m.selected_atm):
                    m.select(m.selected, now.date())   # first price, or the price moved to a new strike
                    self._recentre(force=True)
            opts = []
            for tok in sorted(m.atm_tokens, key=lambda t: m.option_contracts[t].opt):
                c = m.option_contracts[tok]
                q = self.quotes.get(tok, Quote())
                g = self._greeks(c, q, st.ltp, now) if q.ltp > 0 and st.ltp > 0 else {}
                opts.append({"token": tok, "label": c.label(), "strike": c.strike, "opt": c.opt,
                             "expiry": c.expiry.isoformat(), "lot": c.lot, "ltp": q.ltp or None,
                             "bid": q.bid or None, "ask": q.ask or None,
                             "spread_pct": round(q.spread_pct, 2) if q.spread_pct is not None else None,
                             "lot_cost": round((q.ask or q.ltp) * c.lot) if q.ltp else None, **g})
            row = next((r for r in m.rows() if r["name"] == st.info.name), None)
            detail = {"name": st.info.name, "ltp": st.ltp, "pct": row["pct"] if row else None,
                      "buildup": row["buildup"] if row else "—", "tone": row["tone"] if row else "",
                      "lot": st.info.lot, "fut_expiry": st.info.fut_expiry.isoformat(), "options": opts,
                      "compare": self.stock_compare(st.info.name)}
        return m.snapshot(n, detail)

    def stock_compare(self, name: str) -> dict:
        """Which strike to buy for a mover: the same opening-range rule as the indices, applied to the stock.

        Direction from the move (gainer -> CE, loser -> PE); stop at the middle of the stock's
        09:15-09:30 range (Settings: stop rule); the 2R level as the reference target. Strikes 2 ITM .. 2 OTM
        are compared on loss at the stop vs gain at 2R after charges and time decay."""
        m = self.movers
        st, u = m.stocks.get(name), m.universes.get(name)
        if st is None or u is None:
            return {"error": "No option contracts for this stock in the instrument file."}
        if st.ltp <= 0:
            return {"error": "Waiting for a live price."}
        now = self.clock()
        c1 = self.stock_bars.get(name, (None, None))[0]
        day = self.stock_day.get(name) or now.date()
        first = [b for b in (c1.bars if c1 else []) if b.start.astimezone(IST).date() == day
                 and b.start < ist(day, hm(self.s.or_end))]
        if day == now.date() and now < ist(day, hm(self.s.or_end)):
            return {"error": f"The opening range forms until {fmt_both(ist(day, hm(self.s.or_end)))}; "
                             "the strike comparison starts after that."}
        if not first:
            return {"error": "Loading today's candles for the opening range…"}
        orng = OpeningRange(max(b.h for b in first), min(b.l for b in first), len(first), len(first), True)
        pct = st.pct
        side = "CE" if (pct if pct is not None else st.ltp - orng.mid) > 0 else "PE"
        sig = Signal(name, side, now, now, st.ltp, orng.high, orng.low)
        stop, targets, risk = index_levels(sig, orng, self.s)
        word = "up" if side == "CE" else "down"
        base = {"side": side, "or": {"high": round(orng.high, 2), "low": round(orng.low, 2)},
                "stop": stop, "targets": targets}
        if risk <= 0:
            return {**base, "error": f"{name} is {word} on the day but back on the wrong side of the middle of its "
                                     f"opening range ({orng.mid:,.2f}), so there is no {side} setup right now."}
        buf = breakout_margin(orng, self.s)
        beyond = st.ltp > orng.high + buf if side == "CE" else st.ltp < orng.low - buf
        exp, flags = u.choose_expiry(now.date())
        if exp is None:
            return {**base, "error": "No live expiry for this stock."}
        rows, best = compare_strikes(sig, orng, u, exp, self.quotes, st.ltp, now, self.s, "nse", flags, stock=True)
        fallback = None
        if best is None:
            # Same rule with only the size limit relaxed: which strike, and how much risk 1 lot needs.
            size_only = [r for r in rows if r["why_not"] == ["1 lot is over your max loss"]]
            if size_only:
                top = max(r["rr"] for r in size_only)
                near = [r for r in size_only if r["rr"] >= top - 0.05]
                fb = min(near, key=lambda r: (abs(abs(r["delta"]) - 0.55),
                                              r["spread_pct"] if r["spread_pct"] is not None else 99))
                fallback = dict(fb, reason=(
                    f"{name} {fb['strike']:,.0f} {fb['opt']} ({fb['label']}) at about ₹{fb['premium']:,.2f} fits every "
                    f"check except size: 1 lot ({fb['qty']}) loses about ₹{fb['loss']:,.0f} at the stop, more than your "
                    f"₹{self.s.max_loss_per_trade:,.0f} max loss. It would make about ₹{fb['gain']:,.0f} at 2R "
                    f"({fb['rr']:.2f} to 1)."))
        phase = self.cal.phase(now)
        return {**base, "setup": "breakout" if beyond else "inside", "risk": risk, "expiry": exp.isoformat(),
                "rows": rows, "best": best, "fallback": fallback, "live": phase == "open" and day == now.date(),
                "text": (f"{'Bullish' if side == 'CE' else 'Bearish'} on the day, so BUY {side}. "
                         + (f"{name} is {'above' if side == 'CE' else 'below'} its opening range "
                            f"({orng.low:,.2f} to {orng.high:,.2f}): a breakout." if beyond else
                            f"{name} has not broken its opening range ({orng.low:,.2f} to {orng.high:,.2f}) by the "
                            f"rule's margin yet: buying now is early.")
                         + f" Stop if {name} {'falls' if side == 'CE' else 'rises'} to {stop:,.2f} "
                         f"({'far side' if self.s.stop_rule == 'far' else 'middle'} of the range); "
                         f"reference target {targets['2R']:,.2f} (2R).")}

    def odds_json(self, key: str) -> dict:
        st = self.idx[key]
        bt = self.backtests.get(key)
        return {"index": key, "status": self.history_status.get(key),
                "backtest": bt["summary"] if bt else None, "trades": bt["trades"][-60:] if bt else [],
                "mc": st.mc, "mc_running": st.mc_running}

    def reset_for_replay(self) -> None:
        if self.day:
            self.journal_delete_day(self.day)
            self.journal.paper_delete_day(self.day, self.mode)   # a replayed day starts its paper desk afresh too
            self.journal.paper_delete_auto_day(self.day, self.mode)   # its 09:15 straddles are bought again
        self.paper._auto_tried.clear()
        self.day = None
        self.supertrend.reset()
        self.quotes.clear()
        self.opt_bars.clear()
        for st in self.idx.values():
            st.reset_day()
            st.last_tick = None
        for ms in self.movers.stocks.values():
            ms.ltp = ms.vol = 0.0
            ms.fut = Quote()
        self.latencies.clear()

    def journal_delete_day(self, d: date) -> None:
        self.journal.delete_day(d, self.mode)

    def backfill(self, key: str, bars1m: list[Bar]) -> None:
        """Rebuild today's candles from Angel's 1-minute history (after a mid-day restart)."""
        st = self.idx[key]
        now = self.clock()
        st.c1.reset()
        st.c5.reset()
        for b in sorted(bars1m, key=lambda b: b.start):
            if b.start.date() != now.date() or b.start + timedelta(minutes=1) > now:
                continue
            nb = Bar(b.start, b.o, b.h, b.l, b.c, b.v, True, 1)
            st.c1.bars.append(nb)
            bucket = st.c5.bucket(b.start)
            if bucket is None:
                continue
            last = st.c5.bars[-1] if st.c5.bars else None
            if last and last.start == bucket:
                last.h, last.l, last.c = max(last.h, b.h), min(last.l, b.l), b.c
            else:
                st.c5.bars.append(Bar(bucket, b.o, b.h, b.l, b.c, 0.0, False, 5))
        for b in st.c5.bars:
            b.closed = b.start + timedelta(minutes=5) <= now
        if st.c1.bars:
            st.ltp = st.ltp or st.c1.bars[-1].c
        self.log_event("history", f"{st.meta['label']}: rebuilt {len(st.c1.bars)} one-minute bars from today's history.")


def _trade_view(tr: dict | None) -> dict | None:
    if not tr:
        return None
    keep = ("id", "kind", "verdict", "status", "entry_fill", "qty", "lots", "planned_loss", "exit_time",
            "exit_fill", "exit_reason", "pnl", "r_mult", "costs", "live_pnl", "exit_index", "versions",
            "live_versions")
    return {k: tr.get(k) for k in keep}


def rupee(x: float, decimals: int = 0) -> str:
    """Indian digit grouping: 1,60,000."""
    neg = x < 0
    x = abs(x)
    s = f"{x:.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + whole + (("." + frac) if frac else "")


def trail_stop(p: dict, bars5) -> tuple[float, bool]:
    """Trailing exit (tested): once a CLOSED 5-minute candle has taken the index 1R our way, the stop follows 1R behind
    the best closed-candle high (CE) / low (PE). Returns (stop now, trailing?)."""
    sd = 1 if p["side"] == "CE" else -1
    stop0, e, risk = p["stop_index"], p["entry_index"], p["risk_pts"]
    t_sig = datetime.fromisoformat(p["signal_time"])
    best = None
    for b in bars5:
        if b.closed and b.start >= t_sig:
            v = b.h if sd == 1 else b.l
            best = v if best is None else (max(best, v) if sd == 1 else min(best, v))
    if best is None or risk <= 0 or (best - e) * sd < risk:
        return stop0, False
    trail = best - sd * risk
    return (max(stop0, trail) if sd == 1 else min(stop0, trail)), True


def plan_text(p: dict) -> dict:
    """Plain-English pieces of the signal card."""
    c = p["contract"]
    idx = p["index"]
    exp = date.fromisoformat(c["expiry"])
    exit_t = datetime.fromisoformat(p["exit_time"])
    lots, qty, lot = p["lots"], p["qty"], p["lot"]
    falls = "falls to" if p["side"] == "CE" else "rises to"
    tk = p["target_key"]
    head = (f"BUY {idx} {c['strike']:,.0f} {c['opt']} ({exp.day} {exp.strftime('%b')}) "
            f"at about ₹{p['entry_fill']:,.0f}")
    size = (f"{lots} lot{'s' if lots != 1 else ''} ({qty})" if lots else f"0 lots (1 lot = {lot} is over your max loss)")
    loss = p["loss_at_stop"] if lots else p["one_lot_loss"]
    mode = p.get("exit_mode")
    hr = p.get("half_at_r", 1.0)
    half_text, half_lvl = "", 0.0
    if mode in ("hold", "half"):
        half_lvl = p["entry_index"] + (hr * p["risk_pts"] if p["side"] == "CE" else -hr * p["risk_pts"])
        half_text = (f"Version B: when {idx} reaches {half_lvl:,.2f} (+{hr:g}R) sell half, then move the stop on the "
                     f"rest to {p['entry_index']:,.2f} (your entry). "
                     + ("Needs at least 2 lots to split; " if (lots or 1) < 2 else "")
                     + "the journal tracks A and B on every trade.")
    return {
        "half_text": half_text,
        "headline": head,
        "stop_text": f"exit if {idx} {falls} {p['stop_index']:,.2f} (≈ ₹{p['exit_fill_stop']:,.0f})",
        "target_text": (f"none: hold to {exit_t.astimezone(IST):%H:%M} IST unless stopped "
                        f"({tk} for reference: {idx} {p['target_index']:,.2f}, ≈ ₹{p['exit_fill_target']:,.0f})")
                       if mode == "hold" else
                       (f"none: hold to {exit_t.astimezone(IST):%H:%M} IST. Once {idx} is 1R in profit "
                        f"({p['entry_index'] + (p['risk_pts'] if p['side'] == 'CE' else -p['risk_pts']):,.2f}) on a closed "
                        f"5-minute candle, the stop follows {p['risk_pts']:,.0f} points behind the best price")
                       if mode == "trail" else
                       (f"sell half at {idx} {half_lvl:,.2f} (+{hr:g}R), hold the rest to {exit_t.astimezone(IST):%H:%M} "
                        f"IST with its stop moved to entry") if mode == "half" else
                       f"{idx} {p['target_index']:,.2f} (≈ ₹{p['exit_fill_target']:,.0f}) at {tk}",
        "size_text": size,
        "loss_text": f"Max loss ≈ {rupee(loss)} after costs" + ("" if lots else " for 1 lot"),
        "gain_text": (f"At {tk} ≈ +{rupee(p['gain_at_target'])} after costs (no cap: the trade is held)"
                      if mode in ("hold", "half", "trail") else f"At the target ≈ +{rupee(p['gain_at_target'])} after costs"),
        "exit_text": f"Exit by {fmt_both(exit_t)}",
        "entry_text": f"limit ₹{p['entry_limit']:,.2f} · skip above ₹{p['max_entry_premium']:,.2f}",
    }
