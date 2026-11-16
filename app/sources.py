"""Where ticks come from: the live Angel One feed, the synthetic demo, or a replay.

All three feed the same Engine, so a replay behaves exactly like a live day.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import time

import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bs, demo as demomod
from .angel import AngelClient, AngelError, download_master
from .candles import Bar
from .config import DATA_DIR, Secrets, Settings
from .engine import Engine
from .feed import AngelFeed, MODE_SNAP
from .instruments import INDEX_META, VIX_META, Contract, load_contracts, parse_master, save_contracts
from .movers import FnoStock, parse_fno_stocks
from .timeutil import IST, hm, ist, now_ist

log = logging.getLogger(__name__)


class SimClock:
    def __init__(self, start: datetime, speed: float = 10.0):
        self.t = start
        self.speed = speed
        self.playing = True

    def __call__(self) -> datetime:
        return self.t


# --------------------------------------------------------------------- demo


class DemoSource:
    """Plays a synthetic (or rebuilt-from-candles) session second by second."""

    mode = "demo"

    def __init__(self, engine: Engine, settings: Settings, clock: SimClock, day: date = demomod.DEMO_DAY,
                 paths: dict | None = None, prev_close: dict | None = None, vix_level: float | None = None,
                 label: str = "Synthetic demo session (not real prices)", history: bool = True,
                 feed_drop: bool = True):
        self.engine = engine
        self.s = settings
        self.clock = clock
        self.day = day
        self.label = label
        self.paths = paths or {k: demomod.bridge_path(v["anchors"], v["vol"], seed=11 + i)
                               for i, (k, v) in enumerate(demomod.SCENARIOS.items())}
        self.prev_close = prev_close or {k: v["prev_close"] for k, v in demomod.SCENARIOS.items()}
        self.vix_path = demomod.bridge_path([(t, vix_level or v) for t, v in demomod.VIX_ANCHORS], 0.002, 5)
        self.history = history
        self.want: dict[int, set[str]] = {}
        self.drop = (ist(day, hm("11:50")), ist(day, hm("11:51")) + timedelta(seconds=10)) if feed_drop else None
        self._prev_opt: dict[str, float] = {}
        self._oi_base: dict[str, float] = {}
        self._start = ist(day, hm("09:12"))
        self._contracts = demomod.demo_contracts(day if day != demomod.DEMO_DAY else None,
                                                 {k: float(p[0]) for k, p in self.paths.items()},
                                                 set(getattr(engine.cal, "holidays", {}) or {}))
        self._stocks, self._stock_opts = demomod.demo_stocks(day)
        self._stock_by = {s[0].name: s for s in self._stocks}
        self.engine.source_connected = True

    def status(self) -> dict:
        return {"label": self.label, "playing": self.clock.playing, "speed": self.clock.speed,
                "day": self.day.isoformat(), "sim_time": self.clock.t.isoformat()}

    def _on_want(self, want: dict[int, set[str]]):
        self.want = want

    async def start(self):
        e = self.engine
        e.journal.delete_day(self.day, e.mode)  # each demo run starts with a clean day
        e.on_subscriptions = self._on_want
        e.set_contracts(self._contracts)
        for k in INDEX_META:
            e.seed_index(k, float(self.paths[k][0]), self.prev_close[k])
        e.seed_vix(float(self.vix_path[0]), float(self.vix_path[0]) + 0.3)
        e.status_note = self.label
        e.set_stocks([x[0] for x in self._stocks], self._stock_opts,
                     getattr(self, "_stock_label", "fictional demo stocks, not real"))
        real_daily = getattr(self, "_stock_daily", None)
        e.stock_daily.update(real_daily if real_daily is not None else demomod.demo_stock_daily(self._stocks, self.day))
        e.evaluate()
        if self.history:
            for i, k in enumerate(INDEX_META):
                bars = await asyncio.to_thread(demomod.synthetic_history, k, self.day, 120, 3 + i)
                vix = demomod.synthetic_vix(self.day, 120, 3)
                e.set_history(k, bars, vix, "synthetic demo history, not real")
        elif getattr(self, "real_history", False):
            # A rebuilt real day: use the real history saved by the live app (up to the day before), so yesterday's
            # direction, the wide-day skip and the move-size stars are exactly what the live model had that morning.
            vix = [b for b in await asyncio.to_thread(_cached, "VIX") if b.start.date() < self.day]
            for k in INDEX_META:
                bars = [b for b in await asyncio.to_thread(_cached, k) if b.start.date() < self.day]
                if bars:
                    e.set_history(k, bars, vix, f"real Angel One history up to {bars[-1].start:%d %b %Y}")
                else:
                    e.log_event("history", f"{INDEX_META[k]['label']}: no saved history before {self.day:%d %b}, so "
                                           "yesterday's direction is unknown in this replay (both sides allowed).")
        await self.loop()

    def control(self, action: str, speed: float | None = None):
        if action == "play":
            self.clock.playing = True
        elif action == "pause":
            self.clock.playing = False
        elif action == "speed" and speed:
            self.clock.speed = float(max(1, min(speed, 600)))
        elif action == "restart":
            self.engine.reset_for_replay()
            self.clock.t = self._start
            self.clock.playing = True
            self.engine.source_connected = True
            for k in INDEX_META:
                self.engine.seed_index(k, float(self.paths[k][0]), self.prev_close[k])
            self.engine.evaluate()

    async def loop(self):
        self.clock.t = self._start
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            real_dt, last = now - last, now
            if not self.clock.playing:
                continue
            try:
                self.advance(real_dt * self.clock.speed)
            except Exception as ex:             # one bad tick pauses the replay; it no longer kills it (v14.38)
                log.exception("replay step failed")
                self.clock.playing = False
                self.engine.log_event("replay", f"Replay paused at {self.clock.t:%H:%M:%S}: {ex}. Press Play to go on.",
                                      "warn")

    def advance(self, sim_seconds: float):
        t0 = self.clock.t
        t1 = t0 + timedelta(seconds=sim_seconds)
        end = ist(self.day, hm("15:35"))
        if t1 > end:
            t1 = end
            self.clock.playing = False
        s = math.floor(t0.timestamp()) + 1
        last_eval = s
        while s <= t1.timestamp():
            self.clock.t = datetime.fromtimestamp(s, IST)
            self._emit(self.clock.t)
            if s - last_eval >= 5:
                self.engine.evaluate()
                last_eval = s
            s += 1
        self.clock.t = t1
        self.engine.evaluate()

    def _emit(self, t: datetime):
        e = self.engine
        if self.drop and self.drop[0] <= t < self.drop[1]:
            e.source_connected = t < self.drop[0] + timedelta(seconds=20)
            if not e.source_connected:
                e.source_error = "demo: simulated feed drop, reconnecting"
            return
        e.source_connected = True
        e.source_error = ""
        i = int((t - ist(self.day, hm("09:15"))).total_seconds())
        if i < 0 or i >= demomod.N_SEC:
            return
        ms = int(t.timestamp() * 1000)
        rx = t.timestamp()
        for k, meta in INDEX_META.items():
            e.on_tick({"mode": MODE_SNAP, "ws_type": meta["ws_type"], "token": meta["token"], "ts": ms,
                       "ltp": float(self.paths[k][i]), "close": self.prev_close[k],
                       "open": float(self.paths[k][0])}, rx)
        vix = float(self.vix_path[i])
        if i % 5 == 0:
            e.on_tick({"mode": MODE_SNAP, "ws_type": VIX_META["ws_type"], "token": VIX_META["token"],
                       "ts": ms, "ltp": vix, "close": float(self.vix_path[0]) + 0.3}, rx)
        progress = i / demomod.N_SEC
        r = self.s.risk_free_rate_pct / 100.0
        vols = getattr(self, "_stock_vol", None)
        if vols is not None:
            # real stocks: a few ticks a minute, at the open, high/low and close points of each 1-minute candle
            sec = i % 60
            if sec in (0, 10, 20, 30, 40, 50, 59):
                m = i // 60
                for info, pc, path, _, _ in self._stocks:
                    per_min, cum = vols[info.name]
                    v = cum[m] + (per_min[m] * (sec + 1) / 60 if m < len(per_min) else 0.0)
                    e.on_tick({"mode": MODE_SNAP, "ws_type": 1, "token": info.eq_token, "ts": ms,
                               "ltp": float(path[i]), "close": pc, "open": float(path[0]), "vol": int(v)}, rx)
        for j, (info, pc, path, oi_trend, iv) in enumerate(self._stocks if vols is None else ()):
            px = float(path[i])
            if (i + j) % 2 == 0:
                e.on_tick({"mode": MODE_SNAP, "ws_type": 1, "token": info.eq_token, "ts": ms, "ltp": px,
                           "close": pc, "open": float(path[0]), "vol": int(progress * 4.0e7 / pc * (1 + j % 5))}, rx)
            if (i + j) % 3 == 0:
                base = 6.0e6 / pc * info.lot
                oi = base * (1 + oi_trend * progress)
                e.on_tick({"mode": MODE_SNAP, "ws_type": 2, "token": info.fut_token, "ts": ms,
                           "ltp": round(px * 1.002, 2), "close": round(pc * 1.002, 2), "oi": int(oi),
                           "oi_chg_pct": oi_trend * progress * 100}, rx)
        for tok in self.engine.movers.selected_tokens | self.engine.pick_tokens | self.engine.paper.tokens:
            n = int(tok[1:]) if tok[1:].isdigit() else 0
            if (i + n) % 3:
                continue
            c = e.movers.option_contracts.get(tok)
            spec = self._stock_by.get(c.name) if c else None
            if not spec:
                continue
            S = float(spec[2][i])
            T = bs.years_to_expiry(t, c.expiry)
            tk = c.tick or 0.05                      # stock options trade in Rs 0.01 or 0.05 steps
            p = max(tk, bs.price(S, c.strike, T, r, spec[4], c.opt))
            ltp = round(round(p / tk) * tk, 2)
            half = max(tk, round(ltp * 0.006 / tk) * tk)
            pcp = max(tk, round(bs.price(spec[1], c.strike, T + 1 / 365, r, spec[4], c.opt), 2))
            e.on_tick({"mode": MODE_SNAP, "ws_type": 2, "token": tok, "ts": ms, "ltp": ltp, "close": pcp,
                       "oi": 0, "bids": [(max(tk, round(ltp - half, 2)), c.lot * 5, 2)],
                       "asks": [(round(ltp + half, 2), c.lot * 4, 2)]}, rx)
        for wt in (2, 4):
            for tok in self.want.get(wt, ()):
                n = int(tok[1:]) if tok[1:].isdigit() else hash(tok)
                if (i + n) % 3:
                    continue
                c = e.contracts.get(tok)
                if not c:
                    continue
                S = float(self.paths[c.name][i])
                q = demomod.option_quote(c, S, t, vix, r, math.sin(i / 37.0 + n))
                if tok not in self._prev_opt:
                    y = t - timedelta(days=1)
                    pc = demomod.option_quote(c, self.prev_close[c.name], y.replace(hour=15, minute=29),
                                              vix + 0.3, r, 0.0)["ltp"]
                    self._prev_opt[tok] = pc
                    step = INDEX_META[c.name]["step"]
                    s0 = float(self.paths[c.name][0])
                    side = (c.strike - s0) / step if c.opt == "CE" else (s0 - c.strike) / step
                    base = 3.0e6 * math.exp(-((c.strike - s0) / (7 * step)) ** 2)
                    base *= (1.5 if c.strike % (4 * step) == 0 else 1.0) * (1 + 0.5 / (1 + math.exp(-side)))
                    self._oi_base[tok] = base / INDEX_META[c.name]["lot"]
                base = self._oi_base[tok]
                otm = (c.opt == "CE" and c.strike > S) or (c.opt == "PE" and c.strike < S)
                oi = base * (1 + progress * (0.35 if otm else -0.12) + 0.02 * math.sin(i / 300.0 + n))
                lot = c.lot
                oi_q = int(round(oi) * lot)
                vol = int(base * lot * 2.5 * progress * (1.2 if otm else 0.8))
                e.on_tick({"mode": MODE_SNAP, "ws_type": wt, "token": tok, "ts": ms, "ltp": q["ltp"],
                           "close": self._prev_opt[tok], "oi": oi_q,
                           "oi_chg_pct": (oi / base - 1) * 100, "vol": vol,
                           "bids": [(q["bid"], lot * 40, 3)], "asks": [(q["ask"], lot * 35, 3)]}, rx)


# ------------------------------------------------------------------- replay


def gzip_lines(path: Path, chunk: int = 1 << 16):
    """Lines of a recording (gzip, possibly several gzip parts appended), carrying on past damage (v14.36).

    If the app was stopped hard while recording, a part can be cut off in the middle of the file and later parts
    are appended after it. Python's gzip reader stops there with "invalid block type"; this skips the damaged bit
    (a few seconds of ticks at most) and carries on with the next part."""
    import zlib
    magic = b"\x1f\x8b\x08"
    with open(path, "rb") as f:
        buf, tail = b"", b""
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        while True:
            if not buf:
                buf = f.read(chunk)
                if not buf:
                    break
            try:
                out = d.decompress(buf)
                buf = b""
                if d.eof:                                  # one gzip part ended: the rest belongs to the next one
                    buf = d.unused_data
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            except zlib.error:
                tail = b""                                 # a line cut by the damage is dropped
                i = buf.find(magic, 1)
                while i < 0:
                    more = f.read(chunk)
                    if not more:
                        return
                    buf = buf[-2:] + more
                    i = buf.find(magic, 1)
                buf = buf[i:]
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                continue
            if out:
                tail += out
                *lines, tail = tail.split(b"\n")
                for ln in lines:
                    if ln:
                        yield ln.decode("utf-8", "replace")
        if tail.strip():
            yield tail.decode("utf-8", "replace")


def _cached(key: str) -> list[Bar]:
    from .history import load_cached
    try:
        return load_cached(DATA_DIR / "history", key)
    except (OSError, ValueError):
        return []


class ReplaySource(DemoSource):
    """Replays a session recorded from the live feed (data/sessions/<date>/)."""

    mode = "replay"

    def __init__(self, engine: Engine, settings: Settings, clock: SimClock, folder: Path):
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        day = date.fromisoformat(meta["day"])
        self.folder = folder
        self.meta = meta
        for k, rows in (meta.get("context1m") or {}).items():   # earlier days saved by the rebuild
            engine.set_context1m(k, [_bar(r, 1) for r in rows])
        if meta.get("kind") == "rebuilt":
            paths = {k: demomod.path_from_minutes([_bar(r, 1) for r in rows]) for k, rows in meta["bars1m"].items()}
            prev = meta.get("prev_close") or {}
            for k, p in paths.items():
                if k not in prev:              # yesterday's close from the saved history, else the day's first price
                    older = [b for b in _cached(k) if b.start.date() < day]
                    prev[k] = older[-1].c if older else float(p[0])
            super().__init__(engine, settings, clock, day, paths, prev, meta.get("vix"),
                             label=f"Rebuilt from 1-minute candles of {day}; option prices are estimates",
                             history=False, feed_drop=False)
            self.recorded = False
            self.real_history = True
            # no made-up demo stocks in a rebuilt day: the real F&O stocks if the rebuild saved them, else none
            self._stocks, self._stock_opts, self._stock_by = [], [], {}
            if meta.get("stocks"):
                self._load_real_stocks(folder, meta, day)
        else:
            self.engine = engine
            self.s = settings
            self.clock = clock
            self.day = day
            self.label = f"Replay of the recorded live session of {day}"
            self.recorded = True
            self._start = ist(day, hm("09:12"))
            self._contracts = load_contracts(folder / "contracts.json")
            self.want = {}
            engine.source_connected = True

    def _load_real_stocks(self, folder: Path, meta: dict, day: date) -> None:
        def read(name):
            f = folder / name
            try:
                return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            except ValueError:
                return {}

        bars = read("stock_bars1m.json")
        daily = {k: [(date.fromisoformat(r[0]), *r[1:]) for r in v] for k, v in read("stock_daily.json").items()}
        out, vols = [], {}
        for d in meta["stocks"]:
            rows = bars.get(d["name"])
            if not rows:
                continue
            b1 = [_bar(r, 1) for r in rows]
            info = FnoStock(d["name"], d["eq_token"], d["fut_token"], d["fut_symbol"],
                            date.fromisoformat(d["fut_expiry"]), d["lot"])
            older = [r for r in daily.get(info.name, []) if r[0] < day]
            pc = float(older[-1][4]) if older else b1[0].o
            closes = np.array([r[4] for r in older[-21:]], dtype=float)
            iv = float(np.std(np.diff(np.log(closes))) * math.sqrt(252)) if len(closes) > 5 else 0.3
            out.append((info, pc, demomod.path_from_minutes(b1, close_sec=59), 0.0, min(max(iv, 0.18), 0.8)))
            per_min = np.zeros(demomod.N_SEC // 60 + 1)
            for b in b1:
                k = int((b.start - ist(day, hm("09:15"))).total_seconds() // 60)
                if 0 <= k < len(per_min):
                    per_min[k] += b.v
            vols[info.name] = (per_min, np.concatenate([[0.0], np.cumsum(per_min)]))
        opts = []
        if (folder / "stock_options.json").exists():
            try:
                opts = load_contracts(folder / "stock_options.json")
            except (ValueError, KeyError):
                opts = []
        self._stocks, self._stock_opts = out, opts
        self._stock_by = {x[0].name: x for x in out}
        self._stock_vol = vols
        self._stock_daily = {k: v for k, v in daily.items() if k in self._stock_by}
        self._stock_label = "real F&O stocks rebuilt from 1-minute candles; build-up (OI) not available"

    async def start(self):
        if not self.recorded:
            await super().start()
            return
        e = self.engine
        e.journal.delete_day(self.day, e.mode)
        e.on_subscriptions = self._on_want
        e.set_contracts(self._contracts)
        for k, v in (self.meta.get("seed") or {}).items():
            if k == "VIX":
                e.seed_vix(v[0], v[1])
            else:
                e.seed_index(k, v[0], v[1])
        e.status_note = self.label
        st = [FnoStock(d["name"], d["eq_token"], d["fut_token"], d["fut_symbol"], date.fromisoformat(d["fut_expiry"]),
                       d["lot"]) for d in self.meta.get("stocks", [])]
        if st:
            e.set_stocks(st, [], "recorded session")
        e.evaluate()
        self._reader = self._lines()
        self._pending = None
        await self.loop()

    def _lines(self):
        for line in gzip_lines(self.folder / "ticks.jsonl.gz"):   # carries on past a damaged part (v14.37)
            try:
                yield json.loads(line)
            except ValueError:
                continue

    def control(self, action: str, speed: float | None = None):
        if not self.recorded or action != "restart":
            return super().control(action, speed)
        self.engine.reset_for_replay()
        self.clock.t = self._start
        self.clock.playing = True
        self.ended = False
        self._reader = self._lines()
        self._pending = None
        self.engine.evaluate()

    def advance(self, sim_seconds: float):
        if not self.recorded:
            return super().advance(sim_seconds)
        if getattr(self, "ended", False):
            self.clock.playing = False
            return
        t1 = self.clock.t + timedelta(seconds=sim_seconds)
        end = ist(self.day, hm("15:35"))
        if t1 > end:
            t1, self.clock.playing = end, False
        limit = t1.timestamp()
        n = 0
        while True:
            try:
                row = self._pending or next(self._reader, None)
            except Exception as ex:             # a reader error must never stop the app: treat it as the end
                log.warning("recording %s: %s", self.folder, ex)
                row = None
            self._pending = None
            if row is None or row["rx"] > end.timestamp():
                # nothing more recorded in the session: stop at the last tick and say so (v14.38)
                self._pending = row
                self.ended = True
                self.clock.playing = False
                last = self.clock.t
                if last < ist(self.day, hm("15:25")):
                    self.engine.log_event("replay", f"End of recording at {last:%H:%M:%S} IST: the app was closed then. "
                                                    "For the whole day, use Past days > Rebuild & practise.", "warn")
                else:
                    self.engine.log_event("replay", "End of the recorded day.")
                self.engine.evaluate()
                return
            if row["rx"] > limit:
                self._pending = row
                break
            self.clock.t = datetime.fromtimestamp(row["rx"], IST)
            if "status" in row:
                self.engine.source_connected = row["status"] == "connected"
                self.engine.source_error = row.get("error", "")
            else:
                self.engine.on_tick(row["t"], row["rx"])
            n += 1
            if n % 400 == 0:
                self.engine.evaluate()
        self.clock.t = t1
        self.engine.evaluate()


def _bar(row, minutes: int) -> Bar:
    return Bar(datetime.fromisoformat(row[0]).astimezone(IST), row[1], row[2], row[3], row[4],
               row[5] if len(row) > 5 else 0.0, True, minutes)


# --------------------------------------------------------------- recorder


class Recorder:
    """Writes every live tick to data/sessions/<date>/ticks.jsonl.gz for later replay."""

    def __init__(self, root: Path):
        self.root = root
        self.day: date | None = None
        self._f = None
        self._buf: list[str] = []
        self.count = 0

    def folder(self, d: date) -> Path:
        return self.root / d.isoformat()

    def start_day(self, d: date, contracts: list[Contract], seed: dict, stocks: list | None = None) -> None:
        self.close()
        self.day = d
        f = self.folder(d)
        f.mkdir(parents=True, exist_ok=True)
        save_contracts(contracts, f / "contracts.json")
        st = [{"name": s.name, "eq_token": s.eq_token, "fut_token": s.fut_token, "fut_symbol": s.fut_symbol,
               "fut_expiry": s.fut_expiry.isoformat(), "lot": s.lot} for s in (stocks or [])]
        (f / "meta.json").write_text(json.dumps({"day": d.isoformat(), "kind": "recorded", "seed": seed,
                                                 "stocks": st}), encoding="utf-8")
        self._f = gzip.open(f / "ticks.jsonl.gz", "at", encoding="utf-8")

    def tick(self, t: dict, rx: float) -> None:
        if self._f is None:
            return
        now = datetime.fromtimestamp(rx, IST)
        if not (hm("09:00") <= now.time() <= hm("15:35")):
            return
        self._buf.append(json.dumps({"rx": round(rx, 3), "t": t}, separators=(",", ":")))
        self.count += 1

    def status(self, connected: bool, error: str = "") -> None:
        if self._f is not None:
            self._buf.append(json.dumps({"rx": round(time.time(), 3),
                                         "status": "connected" if connected else "disconnected",
                                         "error": error[:120]}))

    def flush(self) -> None:
        if self._f is not None and self._buf:
            self._f.write("\n".join(self._buf) + "\n")
            self._f.flush()
            self._buf.clear()

    def close(self) -> None:
        self.flush()
        if self._f is not None:
            self._f.close()
            self._f = None


# --------------------------------------------------------------------- live


class LiveSource:
    """Angel One: daily TOTP login, instrument file, WebSocket feed, history for the odds."""

    mode = "live"
    broker = "Angel One"

    def __init__(self, engine: Engine, settings: Settings, secrets: Secrets):
        self.engine = engine
        self.s = settings
        self.secrets = secrets
        self.client = self._make_client(secrets)
        self.feed: AngelFeed | None = None
        self.recorder = Recorder(DATA_DIR / "sessions")
        self.session_day: date | None = None
        self.contracts: list[Contract] = []
        self.stocks: list[FnoStock] = []
        self._tasks: list[asyncio.Task] = []
        self._was_connected = None
        self._stock_fetch: dict[str, datetime] = {}
        engine.on_stock_chart = self._stock_chart

    def _stock_chart(self, name: str) -> None:
        """A stock was clicked: load its 1-minute candles for today (or the last session) once a minute at most."""
        now = now_ist()
        last = self._stock_fetch.get(name)
        if last and (now - last).total_seconds() < 60:
            return
        self._stock_fetch[name] = now
        try:
            asyncio.get_running_loop().create_task(self._fill_stock(name))
        except RuntimeError:
            pass

    async def _fill_stock(self, name: str) -> None:
        st = self.engine.movers.stocks.get(name)
        if st is None:
            return
        cal = self.engine.cal
        now = now_ist()
        d = now.date()
        if not cal.is_trading_day(d) or now.time() < hm("09:16"):
            d = cal.prev_trading_day(d)
        end = min(now, ist(d, hm("15:30")))
        try:
            rows = await self.client.candles("NSE", st.info.eq_token, "ONE_MINUTE", ist(d, hm("09:15")), end)
        except AngelError as e:
            log.warning("Could not load %s candles: %s", name, e)
            self.engine.log_event("history", f"{name}: could not load the chart history ({e}). "
                                             "Live candles still build from the feed.", "warn")
            self._stock_fetch.pop(name, None)
            return
        self.engine.backfill_stock(name, [Bar(r[0], r[1], r[2], r[3], r[4], r[5], True, 1) for r in rows], d)

    def _make_client(self, secrets):
        return AngelClient(secrets)

    def _make_feed(self):
        return AngelFeed(self._get_session, self.secrets.api_key.reveal(),
                         self.secrets.client_code.reveal(), self._on_tick)

    def status(self) -> dict:
        return {"label": "Live: Angel One SmartAPI", "recording": str(self.recorder.folder(self.recorder.day))
                if self.recorder.day else None, "ticks_recorded": self.recorder.count,
                "reconnects": self.feed.reconnects if self.feed else 0}

    def control(self, action: str, speed: float | None = None):
        return None

    async def _login(self) -> None:
        delay = 5
        while True:
            try:
                self.engine.status_note = f"Logging in to {self.broker}…"
                await self.client.login()
                self.session_day = now_ist().date()
                self.engine.status_note = ""
                log.info("Logged in to %s", self.broker)
                return
            except AngelError as e:
                self.engine.status_note = str(e)
                self.engine.source_error = str(e)
                log.warning("%s (retrying in %ss)", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _get_session(self, force: bool):
        if force or self.client.session is None or self.session_day != now_ist().date():
            await self._login()
        return self.client.session

    async def _load_instruments(self) -> None:
        today = now_ist().date()
        self.engine.status_note = "Downloading Angel One's instrument file…"
        rows = await download_master(DATA_DIR / "cache", today.isoformat())
        self.contracts = parse_master(rows)
        if not self.contracts:
            raise AngelError("The instrument file had no NIFTY or SENSEX options")
        self.engine.set_contracts(self.contracts)
        self.stocks, stock_opts = parse_fno_stocks(rows, today)
        self.engine.set_stocks(self.stocks, stock_opts, "")
        self.engine.status_note = ""

    async def _seed_quotes(self) -> dict:
        seed = {}
        try:
            req: dict[str, list[str]] = {"NSE": [VIX_META["token"]]}
            for m in INDEX_META.values():
                req.setdefault(m["exchange"], []).append(m["token"])
            rows = await self.client.quote("FULL", req)
            for r in rows:
                tok, ltp, close = str(r.get("symbolToken")), float(r.get("ltp") or 0), float(r.get("close") or 0)
                if tok == VIX_META["token"]:
                    self.engine.seed_vix(ltp, close)
                    seed["VIX"] = [ltp, close]
                for k, m in INDEX_META.items():
                    if tok == m["token"]:
                        self.engine.seed_index(k, ltp, close)
                        seed[k] = [ltp, close]
        except AngelError as e:
            log.warning("Could not read starting quotes: %s", e)
        return seed

    async def _backfill_today(self) -> None:
        now = now_ist()
        if not self.engine.cal.is_trading_day(now.date()) or now.time() < hm("09:16"):
            return
        for k, m in INDEX_META.items():
            for attempt in range(3):
                try:
                    rows = await self.client.candles(m["exchange"], m["token"], "ONE_MINUTE",
                                                     ist(now.date(), hm("09:15")), now)
                except AngelError as e:
                    log.warning("Could not backfill today's %s candles (try %d): %s", k, attempt + 1, e)
                    rows = []
                if rows:
                    self.engine.backfill(k, [Bar(r[0], r[1], r[2], r[3], r[4], r[5], True, 1) for r in rows])
                    break
                await asyncio.sleep(2.0)

    def _on_tick(self, t: dict, rx: float) -> None:
        self.engine.on_tick(t, rx)
        self.recorder.tick(t, rx)

    async def start(self):
        e = self.engine
        await self._login()
        while True:
            try:
                await self._load_instruments()
                break
            except Exception as ex:
                e.status_note = f"Instrument file: {ex}"
                await asyncio.sleep(30)
        seed = await self._seed_quotes()
        e.evaluate()
        await self._backfill_today()
        self.recorder.start_day(now_ist().date(), self.contracts, seed, self.stocks)
        self.feed = self._make_feed()
        e.on_subscriptions = lambda want: self.feed.want(want, MODE_SNAP)
        e._last_subs = {}
        e._recentre(force=True)
        self._tasks = [asyncio.create_task(self.feed.run()), asyncio.create_task(self._housekeeping()),
                       asyncio.create_task(self._load_history()), asyncio.create_task(self._load_prev_oi()),
                       asyncio.create_task(self._load_stock_daily()), asyncio.create_task(self._settle_old_trades()),
                       asyncio.create_task(self._load_context())]
        while True:
            await asyncio.sleep(0.25)
            e.source_connected = bool(self.feed and self.feed.connected)
            e.source_error = self.feed.last_error if self.feed else ""
            if e.source_connected != self._was_connected:
                self.recorder.status(e.source_connected, e.source_error)
                self._was_connected = e.source_connected
            e.evaluate()

    async def _housekeeping(self):
        while True:
            await asyncio.sleep(2)
            self.recorder.flush()
            now = now_ist()
            if self.session_day != now.date() and now.time() >= hm("08:40"):
                log.info("New day: logging in again and reloading the instrument file")
                await self._login()
                try:
                    await self._load_instruments()
                except Exception as ex:
                    log.warning("Instrument reload failed: %s", ex)
                seed = await self._seed_quotes()
                self.recorder.start_day(now.date(), self.contracts, seed, self.stocks)
                self.engine._last_subs = {}
                self.engine._recentre(force=True)
                asyncio.create_task(self._load_prev_oi())

    async def _load_history(self):
        hist_dir = DATA_DIR / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        try:                                   # your saved 1-minute files first, so only newer days download
            from .localdata import seed_history
            await asyncio.to_thread(seed_history, hist_dir)
        except Exception as ex:                # never block the live screen on this
            log.warning("Could not use the saved 1-minute files: %s", ex)
        vix_bars = await self._history_series("VIX", VIX_META["exchange"], VIX_META["token"], hist_dir)
        for k, m in INDEX_META.items():
            bars = await self._history_series(k, m["exchange"], m["token"], hist_dir)
            self.engine.set_history(k, bars, vix_bars, f"real {self.broker} 5-minute history")

    async def _history_series(self, key: str, exchange: str, token: str, folder: Path) -> list[Bar]:
        from .history import load_cached, update_history

        def progress(n, cur):
            if key in self.engine.history_status:
                self.engine.history_status[key] = f"Loading history: {n:,} bars so far (back to {cur:%d %b %Y})"
        try:
            bars, note = await update_history(self.client, key, exchange, token, folder,
                                              self.s.history_max_days, progress)
            if note:
                log.warning("History for %s: %s", key, note)
            return bars
        except AngelError as e:
            if key in self.engine.history_status:
                self.engine.history_status[key] = f"History error: {e}"
            return load_cached(folder, key)

    async def _load_context(self):
        """The last few sessions' 1-minute candles of each index, for the chart's earlier-days view."""
        await asyncio.sleep(8)
        today = now_ist().date()
        for k, m in INDEX_META.items():
            try:
                rows = await self.client.candles(m["exchange"], m["token"], "ONE_MINUTE",
                                                 ist(today - timedelta(days=8), hm("09:15")),
                                                 ist(today - timedelta(days=1), hm("15:30")))
            except AngelError as e:
                log.info("Earlier-days candles for %s unavailable: %s", k, e)
                continue
            keep = set(sorted({r[0].date() for r in rows})[-5:])
            self.engine.set_context1m(k, [Bar(r[0], r[1], r[2], r[3], r[4], r[5], True, 1) for r in rows
                                          if r[0].date() in keep])
            await asyncio.sleep(1.5)

    async def _settle_old_trades(self):
        """Paper trades left open on an earlier day (the app was closed before the stop or 15:00): work out their
        exit from that day's 1-minute candles of the index and the option, so the journal has a result."""
        await asyncio.sleep(90)            # after the morning's other downloads have started
        e = self.engine
        for tr in e.journal.open_before(now_ist().date(), e.mode):
            try:
                p = json.loads(tr["plan_json"])
                c, m = p["contract"], INDEX_META[tr["idx"]]
                d = date.fromisoformat(tr["day"])
                a, b = ist(d, hm("09:15")), ist(d, hm("15:30"))
                idx = await self.client.candles(m["exchange"], m["token"], "ONE_MINUTE", a, b)
                await asyncio.sleep(1.5)
                opt = await self.client.candles(c["exchange"], c["token"], "ONE_MINUTE", a, b)
            except (AngelError, KeyError, ValueError) as ex:
                log.info("Open paper trade %s from %s: could not get its candles (%s); left open", tr["id"], tr["day"], ex)
                continue
            bars = lambda rows: [Bar(r[0], r[1], r[2], r[3], r[4], r[5], True, 1) for r in rows]
            if e.settle_from_candles(tr, bars(idx), bars(opt)) is None:
                log.info("Open paper trade %s from %s: Angel had no option candles for it; left open", tr["id"], tr["day"])
            await asyncio.sleep(1.5)

    async def _load_stock_daily(self):
        """Last ~30 daily candles of every F&O stock (for its normal daily range, used to rank the day's picks).
        One request per stock, paced well inside Angel's limits; cached for the day in data/cache."""
        await asyncio.sleep(5)
        e = self.engine
        today = now_ist().date()
        cache = DATA_DIR / "cache" / f"stock_daily-{today.isoformat()}.json"
        got: dict[str, list] = {}
        if cache.exists():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                got = {k: [(date.fromisoformat(r[0]), *r[1:]) for r in v] for k, v in raw.items()}
            except (ValueError, KeyError):
                got = {}
        e.stock_daily.update(got)
        todo = [s for s in e.movers.stocks.values() if s.info.name not in got]
        # the most recent earlier day's copy: a fallback for any stock Angel keeps refusing today
        fallback: dict[str, list] = {}
        older = sorted(p for p in cache.parent.glob("stock_daily-*.json") if p.name < cache.name)
        if older:
            try:
                raw = json.loads(older[-1].read_text(encoding="utf-8"))
                fallback = {k: [(date.fromisoformat(r[0]), *r[1:]) for r in v] for k, v in raw.items()}
            except (ValueError, KeyError, OSError):
                fallback = {}
        start = ist(today - timedelta(days=50), hm("09:15"))
        end = ist(today, hm("09:00"))
        # Angel refuses candle requests that come too fast (HTTP 403), and the app shares that limit with its
        # other requests: go at about one a second, and give refused stocks two more tries after a pause.
        for attempt in range(3):
            failed = []
            for i, st in enumerate(todo):
                try:
                    rows = await self.client.candles("NSE", st.info.eq_token, "ONE_DAY", start, end)
                except AngelError as ex:
                    failed.append(st)
                    if attempt == 2:
                        log.info("Daily candles for %s unavailable: %s", st.info.name, ex)
                    await asyncio.sleep(3)
                    continue
                got[st.info.name] = [(r[0].date(), r[1], r[2], r[3], r[4]) for r in rows if r[0].date() < today]
                e.stock_daily[st.info.name] = got[st.info.name]
                await asyncio.sleep(1.1)
                if i % 25 == 24:
                    self._save_daily(cache, got)
            self._save_daily(cache, got)
            if not failed:
                break
            if attempt == 2:
                used = [st.info.name for st in failed if fallback.get(st.info.name)]
                for n in used:
                    e.stock_daily[n] = fallback[n]
                if used:
                    log.info("Daily candles: %d stocks still refused; using the previous day's saved ranges for %d",
                             len(failed), len(used))
                break
            log.info("Daily candles: %d stocks refused by Angel (too many requests); trying them again in a minute",
                     len(failed))
            todo = failed
            await asyncio.sleep(60)

    @staticmethod
    def _save_daily(path: Path, got: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: [[r[0].isoformat(), *r[1:]] for r in v] for k, v in got.items()}),
                        encoding="utf-8")

    async def _load_prev_oi(self):
        """Yesterday's closing OI for each chain strike, so change in OI is exact."""
        await asyncio.sleep(20)
        e = self.engine
        today = now_ist().date()
        prev = e.cal.prev_trading_day(today)
        cache = DATA_DIR / "cache" / f"prev_oi-{today.isoformat()}.json"
        if cache.exists():
            e.prev_oi.update(json.loads(cache.read_text(encoding="utf-8")))
        tokens = [t for wt in (2, 4) for t in e._last_subs.get(wt, set()) if t not in e.prev_oi]
        for tok in tokens:
            c = e.contracts.get(tok)
            if not c:
                continue
            try:
                rows = await self.client.oi_data(c.exchange, tok, "FIVE_MINUTE",
                                                 ist(prev, hm("09:15")), ist(prev, hm("15:30")))
                if rows:
                    last = rows[-1]
                    val = last.get("oi") if isinstance(last, dict) else last[-1]
                    if val is not None:
                        e.prev_oi[tok] = int(val)
            except AngelError as ex:
                log.info("Previous OI unavailable (%s); using the feed's OI change instead", ex)
                break
        cache.write_text(json.dumps(e.prev_oi), encoding="utf-8")
