"""Your own saved candle files (history/upstox/1m/*.csv, history/yahoo/1d/*.csv), used before any download.

- Past days: a day inside the files is built from them instantly (no broker request, works offline).
- History for the odds and the backtest: the 5-minute cache is seeded once from the 1-minute files,
  so only the days after the files' last day are downloaded.

Looked for in every <project>/history* folder (history, history1, history2, ...) and in
<project>/../openedge-full/history. Two layouts are understood:
  upstox/1m/<NAME>.csv   1-minute candles (timestamp,open,high,low,close,volume,...)
  <KEY>_5m.json          5-minute candles in the app's own format (KEY = NIFTY, SENSEX, BANKNIFTY, VIX)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .timeutil import IST

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
def _dirs() -> list[Path]:
    out = sorted(p for p in ROOT.glob("history*") if p.is_dir())
    extra = ROOT.parent / "openedge-full" / "history"
    return out + ([extra] if extra.is_dir() else [])
FILES_1M = {"NIFTY": "NIFTY_50.csv", "SENSEX": "SENSEX.csv", "BANKNIFTY": "BANK_NIFTY.csv"}
VIX_1D = "INDIA_VIX.csv"
_days_cache: dict[str, tuple[float, list[str]]] = {}


def path_1m(key: str) -> Path | None:
    name = FILES_1M.get(key)
    for d in _dirs():
        if name and (d / "upstox" / "1m" / name).exists():
            return d / "upstox" / "1m" / name
    return None


def _rows(path: Path, days: set[str]) -> dict[str, list[list]]:
    """{day: [[iso, o, h, l, c, v], ...]} for the given days (ISO dates), read in one pass."""
    out: dict[str, list[list]] = {}
    with path.open("r", encoding="utf-8") as f:
        next(f, None)
        for line in f:
            d = line[:10]
            if d not in days:
                continue
            p = line.rstrip("\n").split(",")
            try:
                out.setdefault(d, []).append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                                              float(p[5] or 0)])
            except (IndexError, ValueError):
                continue
    return out


def days_1m(key: str) -> list[str]:
    """Sorted trading days (ISO) in the 1-minute file of this index."""
    p = path_1m(key)
    if p is None:
        return []
    m = p.stat().st_mtime
    c = _days_cache.get(str(p))
    if c and c[0] == m:
        return c[1]
    seen: list[str] = []
    with p.open("r", encoding="utf-8") as f:
        next(f, None)
        last = ""
        for line in f:
            d = line[:10]
            if d != last:
                seen.append(d)
                last = d
    seen = sorted(set(seen))
    _days_cache[str(p)] = (m, seen)
    return seen


def day_bars_1m(key: str, iso_days: list[str]) -> dict[str, list[list]]:
    """1-minute rows for the given days from the saved files ({} if there is no file)."""
    p = path_1m(key)
    if p is None or not iso_days:
        return {}
    have = set(days_1m(key))
    want = {d for d in iso_days if d in have}
    return _rows(p, want) if want else {}


def vix_close(day: date) -> float | None:
    for d in _dirs():
        p = d / "yahoo" / "1d" / VIX_1D
        if not p.exists():
            continue
        best = None
        with p.open("r", encoding="utf-8") as f:
            next(f, None)
            for line in f:
                if line[:10] > day.isoformat():
                    break
                try:
                    best = float(line.split(",")[4])
                except (IndexError, ValueError):
                    pass
        return best
    return None


def local_rebuild(day: date, context_days: int = 3) -> dict | None:
    """The rebuilt-day record (same shape as `python -m app rebuild`) from the saved files, or None
    if any index is missing that day or the day is incomplete."""
    iso = day.isoformat()
    out = {"day": iso, "kind": "rebuilt", "source": "local 1-minute files", "bars1m": {}, "prev_close": {},
           "context1m": {}}
    for k in FILES_1M:
        p = path_1m(k)
        if p is None:
            return None
        all_days = days_1m(k)
        if iso not in all_days:
            return None
        i = all_days.index(iso)
        before = all_days[max(0, i - context_days):i]
        rows = _rows(p, {iso, *before})
        today = rows.get(iso) or []
        if len(today) < 300 or today[-1][0][11:16] < "15:20":
            return None                                  # a partial day: let the broker fill it instead
        out["bars1m"][k] = today
        ctx = [r for d in before for r in rows.get(d, [])]
        out["context1m"][k] = ctx
        if before and rows.get(before[-1]):
            out["prev_close"][k] = rows[before[-1]][-1][4]
    out["vix"] = vix_close(day)
    return out


def seed_history(folder: Path, keys=(*FILES_1M, "VIX")) -> list[str]:
    """Merge your files into data/history/<KEY>_5m.json, once per change of those files: the 5-minute
    JSON files as they are, the 1-minute CSV files grouped into 5-minute bars. Returns the keys seeded."""
    done = []
    for k in keys:
        p = path_1m(k)
        five = [d / f"{k}_5m.json" for d in _dirs() if (d / f"{k}_5m.json").exists()]
        if p is None and not five:
            continue
        cache = folder / f"{k}_5m.json"
        meta_path = folder / f"{k}_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        stamp = ";".join(f"{q}:{q.stat().st_size}:{int(q.stat().st_mtime)}" for q in ([p] if p else []) + five)
        if meta.get("seeded_local") == stamp:
            continue
        rows: dict[str, list] = {}
        if cache.exists():
            try:
                for r in json.loads(cache.read_text(encoding="utf-8")):
                    rows[r[0]] = r
            except ValueError:
                rows = {}
        for q in five:                                     # 5-minute files in the app's own format
            try:
                for r in json.loads(q.read_text(encoding="utf-8")):
                    if isinstance(r, list) and len(r) >= 5:
                        rows.setdefault(r[0], [r[0], *[float(x or 0) for x in r[1:6]]] + ([0.0] if len(r) < 6 else []))
            except (ValueError, OSError) as ex:
                log.warning("Could not read %s: %s", q, ex)
        cur = None
        with (p.open("r", encoding="utf-8") if p else open(__file__, encoding="utf-8")) as f:
            if not p:
                f.seek(0, 2)                               # no 1-minute file for this key: nothing to read
            next(f, None)
            for line in f:
                q = line.rstrip("\n").split(",")
                try:
                    t = datetime.fromisoformat(q[0]).astimezone(IST)
                    o, h, l, c, v = float(q[1]), float(q[2]), float(q[3]), float(q[4]), float(q[5] or 0)
                except (IndexError, ValueError):
                    continue
                b = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)
                if cur and cur[0] == b:
                    cur[2], cur[3], cur[4], cur[5] = max(cur[2], h), min(cur[3], l), c, cur[5] + v
                else:
                    if cur:
                        rows.setdefault(cur[0].isoformat(), [cur[0].isoformat(), *cur[1:]])
                    cur = [b, o, h, l, c, v]
        if cur:
            rows.setdefault(cur[0].isoformat(), [cur[0].isoformat(), *cur[1:]])
        folder.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([rows[x] for x in sorted(rows)]), encoding="utf-8")
        meta["seeded_local"] = stamp
        meta["exhausted"] = True          # nothing older is needed from the broker
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        log.info("History for %s: seeded from your files (%d five-minute bars)", k, len(rows))
        done.append(k)
    return done
