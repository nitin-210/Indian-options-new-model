"""Start the app.

    python -m app                 live if config/secrets.toml is filled in, otherwise the demo
    python -m app --demo          the synthetic demo session (works any time)
    python -m app --replay PATH   replay a recorded day, e.g. data/sessions/2026-11-09
    python -m app check-login     test your Angel One login, data access and live feed
    python -m app rebuild DATE    save a past day's 1-minute candles (indices and F&O stocks) for replay
    python -m app backtest        test the rule on Angel One's past 5-minute history and print a report

The server only listens on 127.0.0.1 (this computer). There is no order placement code.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR, HOLIDAYS_PATH, SECRETS_PATH, load_secrets, load_settings, setup_logging
from .timeutil import Calendar, hm, ist, now_ist

log = logging.getLogger("app")


def build_runtime(mode: str, settings, replay_path: Path | None = None, speed: float = 10.0):
    from .engine import Engine, Hub
    from .journal import Journal
    from .server import Runtime
    from .sources import DemoSource, LiveSource, ReplaySource, SimClock
    from .demo import DEMO_DAY

    hub = Hub()
    journal = Journal(DATA_DIR / "journal.db")
    cal = Calendar.load(HOLIDAYS_PATH)
    if mode == "live":
        engine = Engine(settings, cal, journal, "live", hub, now_ist)
        up = _upstox_secrets()
        if up is not None and not up.problems():
            from .upstox import make_live_source
            source = make_live_source(engine, settings, up)
        else:
            source = LiveSource(engine, settings, load_secrets())
    elif mode == "replay":
        clock = SimClock(ist(DEMO_DAY, hm("09:12")), speed)
        engine = Engine(settings, cal, journal, "replay", hub, clock)
        source = ReplaySource(engine, settings, clock, replay_path)
        clock.t = source._start
    else:
        clock = SimClock(ist(DEMO_DAY, hm("09:12")), speed)
        engine = Engine(settings, cal, journal, "demo", hub, clock)
        source = DemoSource(engine, settings, clock)
    return Runtime(settings, engine, source, hub, journal, settings.port)


def _upstox_secrets():
    from .upstox import load_upstox_secrets
    return load_upstox_secrets()


def _data_client():
    """The market-data client: Upstox if its token is in secrets.toml, else Angel One, else None."""
    up = _upstox_secrets()
    if up is not None and not up.problems():
        from .upstox import UpstoxClient
        return UpstoxClient(up)
    from .angel import AngelClient
    s = load_secrets()
    if s is None or s.problems():
        return None
    return AngelClient(s)


def serve(rt, open_browser: bool):
    import uvicorn
    from .server import create_app

    app = create_app(rt)
    url = f"http://127.0.0.1:{rt.port}/"
    from . import BUILD
    print(f"\n  India options signals, build {BUILD} ({rt.engine.mode}) → {url}")
    print("  Signals only · not financial advice · nothing is placed.")
    print("  To stop: press Ctrl+C here (or just close this window).\n")
    if open_browser:
        # An open dashboard tab reconnects by itself within a few seconds of a restart: only open a new tab if none did,
        # so restarts don't pile up tabs (each open tab holds one of the browser's 6 connections to the app).
        def _open():
            if not rt.hub.queues:
                webbrowser.open(url)
            else:
                print("  A dashboard tab is already open and has reconnected; not opening another.\n")
        threading.Timer(4.0, _open).start()
    # log_config=None: uvicorn uses this app's own (filtered, redacted) log handlers.
    # timeout_graceful_shutdown: Ctrl+C must not wait for the browser's live stream to close.
    config = uvicorn.Config(app, host="127.0.0.1", port=rt.port, log_config=None, access_log=False,
                            timeout_graceful_shutdown=3)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            rt.stopping = True  # lets the browser streams finish straight away
            if not getattr(self, "_watchdog", None):
                print("\n  Stopping…")

                def hard_exit():
                    print("  Stopped.")
                    os._exit(0)  # last resort: never leave the app stuck after Ctrl+C

                self._watchdog = threading.Timer(5.0, hard_exit)
                self._watchdog.daemon = True
                self._watchdog.start()
            super().handle_exit(sig, frame)

    Server(config).run()
    print("  Stopped.")


# ------------------------------------------------------------- check-login


async def check_login() -> int:
    up = _upstox_secrets()
    if up is not None:
        from .upstox import check_login_upstox
        return await check_login_upstox(up)
    from .angel import AngelClient, AngelError, download_master
    from .feed import AngelFeed
    from .instruments import INDEX_META, VIX_META, OptionUniverse, parse_master

    def ok(msg):
        print(f"  PASS  {msg}")

    def bad(msg):
        print(f"  FAIL  {msg}")

    print("\nChecking your Angel One setup (nothing secret is printed)…\n")
    secrets = load_secrets()
    if secrets is None:
        bad(f"No keys found. Copy config/secrets.example.toml to {SECRETS_PATH.name} and fill it in.")
        return 1
    probs = secrets.problems()
    if probs:
        for p in probs:
            bad(f"secrets.toml: {p}")
        return 1
    ok("secrets.toml has all four values")
    client = AngelClient(secrets)
    try:
        await client.login()
        ok("Logged in with client code, PIN and TOTP")
        if secrets.historical_api_key:
            (ok if client.hist_session else bad)("Second (historical data) API key " +
                                                  ("logged in" if client.hist_session else "could not log in"))
        rows = await download_master(DATA_DIR / "cache", now_ist().date().isoformat())
        contracts = parse_master(rows)
        for k in INDEX_META:
            u = OptionUniverse(k, contracts)
            exp, flags = u.choose_expiry(now_ist().date())
            if exp:
                ok(f"{k}: next expiry {exp:%a %d %b %Y}, lot size {u.contracts[0].lot}, "
                   f"strike step {u.step(exp):g}" + (f"  ({flags[0]})" if flags else ""))
            else:
                bad(f"{k}: no option contracts found in the instrument file")
        req: dict[str, list[str]] = {"NSE": [VIX_META["token"]]}
        for m in INDEX_META.values():
            req.setdefault(m["exchange"], []).append(m["token"])
        q = await client.quote("LTP", req)
        names = {m["token"]: m["label"] for m in INDEX_META.values()}
        names[VIX_META["token"]] = "India VIX"
        for r in q:
            ok(f"Quote {names.get(str(r.get('symbolToken')), r.get('tradingSymbol'))}: {r.get('ltp')}")
        if not q:
            bad("Quote request returned nothing")
        end = now_ist()
        start = end.replace(hour=9, minute=15)
        from datetime import timedelta
        c = await client.candles("NSE", INDEX_META["NIFTY"]["token"], "FIVE_MINUTE", start - timedelta(days=5), end)
        (ok if c else bad)(f"Historical candles: {len(c)} NIFTY 5-minute bars for the last few days")
        ticks = {"n": 0}

        async def get_session(force):
            return client.session

        feed = AngelFeed(get_session, secrets.api_key.reveal(), secrets.client_code.reveal(),
                         lambda t, rx: ticks.__setitem__("n", ticks["n"] + 1))
        want: dict[int, set[str]] = {VIX_META["ws_type"]: {VIX_META["token"]}}
        for m in INDEX_META.values():
            want.setdefault(m["ws_type"], set()).add(m["token"])
        feed.want(want)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(12)
        feed.stop()
        task.cancel()
        if feed.connected or ticks["n"]:
            ok(f"Live feed connected; {ticks['n']} ticks in 12 s" +
               ("" if ticks["n"] else " (none is normal when the market is closed)"))
        else:
            bad(f"Live feed did not connect: {feed.last_error or 'no reply'}")
        print("\nAll done. Start the app with:  .\\.venv\\Scripts\\python.exe -m app\n")
        return 0
    except AngelError as e:
        bad(str(e))
        return 1
    finally:
        await client.close()


async def rebuild(day: date, with_stocks: bool = True) -> int:
    import json
    from .angel import AngelClient, AngelError, download_master
    from .instruments import INDEX_META, VIX_META, save_contracts
    from .movers import parse_fno_stocks
    from .localdata import local_rebuild

    loc = local_rebuild(day)          # your saved 1-minute files first: no download needed
    if loc is not None:
        f = DATA_DIR / "sessions" / f"{day.isoformat()}-rebuilt"
        f.mkdir(parents=True, exist_ok=True)
        old = {}
        if (f / "meta.json").exists():
            try:
                old = json.loads((f / "meta.json").read_text(encoding="utf-8"))
            except ValueError:
                old = {}
        loc.update({k: v for k, v in old.items() if k in ("stocks",)})
        (f / "meta.json").write_text(json.dumps(loc), encoding="utf-8")
        print(f"  {day}: built from your saved 1-minute files (no download).")
        if not with_stocks:
            print(f"Saved. Replay it with:  .venv/bin/python -m app --replay {f}")
            return 0

    client = _data_client()
    if client is None:
        print("Fill in config/secrets.toml first (see python -m app check-login).")
        return 1
    download_master = getattr(client, "download_master", None) or download_master
    folder = DATA_DIR / "sessions" / f"{day.isoformat()}-rebuilt"
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / "meta.json"
    t0, t1 = ist(day, hm("09:15")), ist(day, hm("15:30"))

    async def fetch(label, exchange, token, interval="ONE_MINUTE", start=t0, end=t1, waits=(0, 30, 60, 120)):
        # Angel refuses bursts of candle requests (HTTP 403) for a while: wait and try again
        last = None
        for wait in waits:
            if wait:
                print(f"\n  Angel One is limiting requests; trying {label} again in {wait} s…")
                await asyncio.sleep(wait)
            try:
                return await client.candles(exchange, token, interval, start, end)
            except AngelError as e:
                last = e
        raise last

    try:
        await client.login()
        old = {}
        if meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                old = {}
        bars = old.get("bars1m") or {}
        if all(k in bars and bars[k] and bars[k][-1][0][11:16] >= "15:25" for k in INDEX_META) and "vix" in old:
            out = old
            print("  Index candles already saved for this day.")
        else:
            out = {"day": day.isoformat(), "kind": "rebuilt", "bars1m": {}, "prev_close": {}}
            for k, m in INDEX_META.items():
                print(f"  {m['label']}: downloading 1-minute candles…")
                rows = await fetch(m["label"], m["exchange"], m["token"])
                if not rows:
                    print(f"No 1-minute candles for {k} on {day}.")
                    return 1
                out["bars1m"][k] = [[r[0].isoformat(), r[1], r[2], r[3], r[4], r[5]] for r in rows]
                await asyncio.sleep(1.5)
            v = await fetch("India VIX", VIX_META["exchange"], VIX_META["token"])
            out["vix"] = v[0][4] if v else None
        if not out.get("context1m"):
            # the 3 sessions before the day, 1-minute, so the replay can show earlier days (liquidity levels)
            out["context1m"] = {}
            for k, m in INDEX_META.items():
                print(f"  {m['label']}: earlier days for the chart…")
                try:
                    rows = await fetch(m["label"], m["exchange"], m["token"], "ONE_MINUTE",
                                       ist(day - timedelta(days=8), hm("09:15")), ist(day - timedelta(days=1), hm("15:30")),
                                       waits=(0, 30))
                except AngelError as e:
                    print(f"    (none: {e})")
                    rows = []
                keep = set(sorted({r[0].date() for r in rows})[-3:])
                out["context1m"][k] = [[r[0].isoformat(), r[1], r[2], r[3], r[4], r[5]] for r in rows
                                       if r[0].date() in keep]
                await asyncio.sleep(1.0)
        meta_path.write_text(json.dumps(out), encoding="utf-8")
        if with_stocks:
            await _rebuild_stocks(client, day, folder, out, fetch, json, download_master, parse_fno_stocks,
                                  save_contracts, AngelError)
            meta_path.write_text(json.dumps(out), encoding="utf-8")
        print(f"Saved. Replay it with:  .\\.venv\\Scripts\\python.exe -m app --replay {folder}")
        return 0
    except AngelError as e:
        print(f"\nFailed: {e}")
        print("Anything downloaded so far is saved; run the same command again later and it carries on from there.")
        return 1
    finally:
        await client.close()


async def _rebuild_stocks(client, day, folder, out, fetch, json, download_master, parse_fno_stocks,
                          save_contracts, AngelError) -> None:
    """Every F&O stock's 1-minute candles for the day, plus the daily candles used for its normal range,
    so the replay can rank the day's picks and movers the way the live app did. Saves as it goes."""
    print("  Stocks: reading Angel One's instrument list…")
    rows = await download_master(DATA_DIR / "cache", now_ist().date().isoformat())
    stocks, opts = parse_fno_stocks(rows, day)
    del rows
    out["stocks"] = [{"name": s.name, "eq_token": s.eq_token, "fut_token": s.fut_token, "fut_symbol": s.fut_symbol,
                      "fut_expiry": s.fut_expiry.isoformat(), "lot": s.lot} for s in stocks]

    def load(name):
        p = folder / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}

    def save(name, data):
        (folder / name).write_text(json.dumps(data), encoding="utf-8")

    # daily candles (normal daily range): the live app's copy for that morning if there is one, else download
    daily = load("stock_daily.json")
    cache = DATA_DIR / "cache" / f"stock_daily-{day.isoformat()}.json"
    if cache.exists():
        try:
            for k, v in json.loads(cache.read_text(encoding="utf-8")).items():
                daily.setdefault(k, v)
        except ValueError:
            pass
    bars = load("stock_bars1m.json")
    jobs = [("daily", s) for s in stocks if s.name not in daily] + [("1m", s) for s in stocks if s.name not in bars]
    total = len(jobs)
    if total:
        print(f"  Stocks: {len(stocks)} F&O stocks, {total} downloads to do (about {total * 1.3 / 60:.0f} min)…")
    done = 0
    for attempt in range(3):
        failed = []
        for kind, s in jobs:
            try:
                if kind == "daily":
                    got = await fetch(s.name, "NSE", s.eq_token, "ONE_DAY", ist(day - timedelta(days=50), hm("09:15")),
                                      ist(day, hm("09:00")), waits=(0,))
                    daily[s.name] = [[r[0].date().isoformat(), r[1], r[2], r[3], r[4]] for r in got
                                     if r[0].date() < day]
                else:
                    got = await fetch(s.name, "NSE", s.eq_token, waits=(0,))
                    bars[s.name] = [[r[0].isoformat(), r[1], r[2], r[3], r[4], r[5]] for r in got]
                done += 1
            except AngelError:
                failed.append((kind, s))
                await asyncio.sleep(3)
            print(f"\r  Stocks: {done}/{total} downloaded" + (f", {len(failed)} refused (retried later)"
                                                             if failed else "") + "      ", end="", flush=True)
            if done % 20 == 0:
                save("stock_daily.json", daily)
                save("stock_bars1m.json", bars)
            await asyncio.sleep(1.1)
        save("stock_daily.json", daily)
        save("stock_bars1m.json", bars)
        if not failed:
            break
        jobs = failed
        if attempt < 2:
            print(f"\n  Angel One refused {len(failed)} requests (too many too fast); trying those again in 60 s…")
            await asyncio.sleep(60)
    if total:
        print()
    have = [s for s in stocks if bars.get(s.name)]
    missing = len(stocks) - len(have)
    # the option strikes a replay can need: nearest two expiries, within 15% of the day's range
    keep = []
    lo_hi = {}
    for s in have:
        b = bars[s.name]
        lo_hi[s.name] = (min(r[3] for r in b) * 0.85, max(r[2] for r in b) * 1.15)
    for c in opts:
        r = lo_hi.get(c.name)
        if r and r[0] <= c.strike <= r[1]:
            keep.append(c)
    save_contracts(keep, folder / "stock_options.json")
    print(f"  Stocks: {len(have)} saved" + (f" ({missing} had no candles or were refused)" if missing else "") + ".")


async def backtest_cmd(offline: bool, days: int | None) -> int:
    from .backtest import format_report, run, write_csv
    from .history import load_cached, update_history
    from .instruments import INDEX_META, VIX_META

    settings = load_settings()
    if days:
        settings.history_max_days = days
    folder = DATA_DIR / "history"
    series = {}
    from .localdata import seed_history
    if seed_history(folder):
        print("History seeded from your saved 1-minute files.")
    if offline:
        for k in ("VIX", *INDEX_META):
            series[k] = load_cached(folder, k)
    else:
        from .angel import AngelError
        client = _data_client()
        if client is None:
            print("Fill in config/secrets.toml first (python -m app check-login), or use --offline.")
            return 1
        try:
            await client.login()
            print("Logged in. Downloading 5-minute history (first time: a few minutes; later: seconds)…")
            metas = {**INDEX_META, "VIX": VIX_META}
            for k, m in metas.items():
                def progress(n, cur, k=k):
                    print(f"\r  {k:<7} {n:>7,} new bars, back to {cur:%d %b %Y}      ", end="", flush=True)
                try:
                    series[k], note = await update_history(client, k, m["exchange"], m["token"], folder,
                                                           settings.history_max_days, progress)
                except AngelError as e:
                    series[k], note = load_cached(folder, k), f"download failed ({e})"
                first = f", from {series[k][0].start:%d %b %Y}" if series[k] else ""
                print(f"\r  {k:<7} {len(series[k]):>7,} bars saved{first}" + (f"  · note: {note}" if note else "") + " " * 10)
        except AngelError as e:
            print(f"Failed: {e}")
            return 1
        finally:
            await client.close()
    today = now_ist().date()
    meta = {k: (int(m["lot"]), float(m["step"]), m["txn"]) for k, m in INDEX_META.items()}
    results = []
    for k in INDEX_META:
        if not series.get(k):
            print(f"No history for {k}.")
            continue
        lot, step, exch = meta[k]
        results.append(run(k, series[k], series.get("VIX", []), settings, lot, step, exch, today))
    text = format_report(results, settings)
    print(text)
    out = DATA_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"backtest-{today.isoformat()}.txt").write_text(text, encoding="utf-8")
    write_csv(results, out / f"backtest-{today.isoformat()}-trades.csv")
    print(f"\nSaved: data\\reports\\backtest-{today.isoformat()}.txt and the trade list as a .csv")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", choices=["run", "check-login", "rebuild", "backtest"], default="run")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD for rebuild")
    p.add_argument("--indices-only", action="store_true", help="rebuild: skip the F&O stocks (faster)")
    p.add_argument("--demo", action="store_true", help="run the synthetic demo session")
    p.add_argument("--replay", type=Path, help="replay a folder under data/sessions")
    p.add_argument("--speed", type=float, default=10.0, help="demo/replay speed (default 10x)")
    p.add_argument("--port", type=int, help="port on 127.0.0.1 (default from settings, 8765)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--offline", action="store_true", help="backtest: use only the cached history")
    p.add_argument("--days", type=int, help="backtest: how many days of history to fetch the first time")
    a = p.parse_args(argv)
    setup_logging()
    if a.command == "check-login":
        return asyncio.run(check_login())
    if a.command == "backtest":
        return asyncio.run(backtest_cmd(a.offline, a.days))
    if a.command == "rebuild":
        if not a.date:
            print("Give a date, e.g.  python -m app rebuild 2026-11-05")
            return 2
        return asyncio.run(rebuild(date.fromisoformat(a.date), with_stocks=not a.indices_only))
    settings = load_settings()
    if a.port:
        settings.port = a.port
    if a.replay:
        mode = "replay"
    elif a.demo:
        mode = "demo"
    else:
        s = load_secrets()
        up = _upstox_secrets()
        if up is not None and up.problems():
            for prob in up.problems():
                print(f"  - secrets.toml: {prob}")
        if up is not None and not up.problems():
            print("Using Upstox for market data (the [upstox] token in config/secrets.toml).")
            mode = "live"
        elif s is None or s.problems():
            print("No broker keys (Upstox or Angel One) in config/secrets.toml yet, so starting the DEMO session.")
            if s is not None:
                for prob in s.problems():
                    print(f"  - secrets.toml: {prob}")
            mode = "demo"
        else:
            mode = "live"
    rt = build_runtime(mode, settings, a.replay, a.speed)
    try:
        serve(rt, not a.no_browser)
    except KeyboardInterrupt:
        pass
    finally:
        rt.journal.close()
        if mode == "live":
            rt.source.recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
