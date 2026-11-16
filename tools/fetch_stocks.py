"""Download 15-minute history for large NSE stocks, for research only (read-only, no orders).

Run from the india-options folder:
    .\\.venv\\Scripts\\python.exe C:\\Users\\hemar\\Downloads\\fetch_stocks.py

Saves data\\history\\stocks\\<NAME>_15m.json (same format as the NIFTY file). It does not change the app,
the strategy or your settings. Safe to stop with Ctrl+C and run again: finished stocks are skipped.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date, datetime

sys.path.insert(0, os.getcwd())  # the india-options folder

try:
    from app.angel import AngelClient, AngelError, download_master
    from app.config import DATA_DIR, load_secrets
    from app.timeutil import now_ist
except ImportError:
    print("Run this from the india-options folder (cd C:\\Users\\hemar\\Documents\\day-trading\\india-options).")
    sys.exit(1)

# NIFTY 50 members today plus stocks that were in it during 2022-2026 (for market breadth).
NAMES = """ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL
BPCL BRITANNIA CIPLA COALINDIA DIVISLAB DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO
HINDALCO HINDUNILVR ICICIBANK INDIGO INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT LTIM M&M MARUTI MAXHEALTH
NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATAMOTORS TMPV TATASTEEL
TCS TECHM TITAN TRENT ULTRACEMCO UPL WIPRO""".split()

DAYS_BACK = 1560          # back to about Aug 2022, like the NIFTY history
CHUNK_DAYS = 180          # Angel allows up to 200 days of 15-minute candles per request
START_BY = date(2022, 9, 15)


async def main() -> int:
    secrets = load_secrets()
    if secrets is None or secrets.problems():
        print("config/secrets.toml is not filled in. Run: .\\.venv\\Scripts\\python.exe -m app check-login")
        return 1
    out_dir = DATA_DIR / "history" / "stocks"
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Reading Angel One's instrument list…")
    rows = await download_master(DATA_DIR / "cache", now_ist().date().isoformat())
    tokens = {}
    for r in rows:
        sym = r.get("symbol", "")
        if r.get("exch_seg") == "NSE" and sym.endswith("-EQ"):
            tokens[sym[:-3]] = str(r["token"])
    todo = [n for n in NAMES if n in tokens]
    missing = [n for n in NAMES if n not in tokens]
    if missing:
        print("Not in the instrument list (renamed or delisted), skipped:", ", ".join(missing))
    client = AngelClient(secrets)
    manifest = {}
    t0 = time.time()
    try:
        await client.login()
        print(f"Logged in. Downloading {len(todo)} stocks (about {len(todo) * 9 * 1.3 / 60:.0f} minutes)…")
        for i, name in enumerate(todo, 1):
            path = out_dir / f"{name}_15m.json"
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if old and datetime.fromisoformat(old[0][0]).date() <= START_BY:
                    print(f"  [{i:2d}/{len(todo)}] {name:<12} already downloaded ({len(old):,} bars)")
                    manifest[name] = {"token": tokens[name], "bars": len(old), "status": "done"}
                    continue
            try:
                got, status = await client.candles_paged("NSE", tokens[name], "FIFTEEN_MINUTE", DAYS_BACK,
                                                         CHUNK_DAYS, now_ist(), pace=1.2)
            except AngelError as e:
                got, status = [], f"stopped: {e}"
            if got:
                path.write_text(json.dumps([[r[0].isoformat(), r[1], r[2], r[3], r[4], r[5]] for r in got]),
                                encoding="utf-8")
            first = got[0][0].date().isoformat() if got else "-"
            print(f"  [{i:2d}/{len(todo)}] {name:<12} {len(got):>6,} bars from {first}  {status if status != 'done' else ''}")
            manifest[name] = {"token": tokens[name], "bars": len(got), "status": status}
            if status.startswith("stopped"):
                await asyncio.sleep(15)   # Angel refused: pause before the next stock
    finally:
        await client.close()
        (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    ok = sum(1 for m in manifest.values() if m["bars"])
    print(f"Done in {(time.time() - t0) / 60:.1f} min: {ok} of {len(todo)} stocks saved in {out_dir}")
    print("If any stock says 'stopped', run the same command again later; finished stocks are skipped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to carry on.")
