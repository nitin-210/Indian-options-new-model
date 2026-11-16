"""Download and cache Angel One 5-minute history (data/history/<KEY>_5m.json)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .angel import AngelClient, AngelError
from .candles import Bar
from .timeutil import IST, now_ist

log = logging.getLogger(__name__)


def _bar(row) -> Bar:
    return Bar(datetime.fromisoformat(row[0]).astimezone(IST), row[1], row[2], row[3], row[4],
               row[5] if len(row) > 5 else 0.0, True, 5)


def load_cached(folder: Path, key: str) -> list[Bar]:
    path = folder / f"{key}_5m.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [_bar(r) for r in rows]


async def update_history(client: AngelClient, key: str, exchange: str, token: str, folder: Path,
                         max_days: int, progress=None) -> tuple[list[Bar], str]:
    """Bring the cache up to date and extend it back towards max_days. Returns (bars, note).

    Progress is saved after every step, so if Angel refuses a request part-way (rate limit or
    the edge of its history), nothing already downloaded is lost and the next run carries on.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}_5m.json"
    meta_path = folder / f"{key}_meta.json"
    rows: dict[str, list] = {}
    if path.exists():
        for r in json.loads(path.read_text(encoding="utf-8")):
            rows[r[0]] = r
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    notes = []

    def merge(new):
        for r in new:
            rows[r[0].isoformat()] = [r[0].isoformat(), r[1], r[2], r[3], r[4], r[5]]
        path.write_text(json.dumps([rows[k] for k in sorted(rows)]), encoding="utf-8")

    end = now_ist()
    if rows:  # 1) the days since the last cached bar
        last = datetime.fromisoformat(max(rows)).astimezone(IST)
        new, status = await client.candles_paged(exchange, token, "FIVE_MINUTE", max(1, (end - last).days + 1),
                                                 90, end, progress)
        merge(new)
        if status.startswith("stopped"):
            notes.append(f"latest days not updated ({status[9:]})")
    # 2) further back, until max_days or until Angel has nothing older
    target = end - timedelta(days=max_days)
    first = datetime.fromisoformat(min(rows)).astimezone(IST) if rows else end
    if not meta.get("exhausted") and first - target > timedelta(days=5):
        new, status = await client.candles_paged(exchange, token, "FIVE_MINUTE", (first - target).days, 90,
                                                 first - timedelta(minutes=1), progress)
        merge(new)
        if status == "exhausted":
            meta["exhausted"] = True
        elif status.startswith("stopped"):
            # Refused at the same oldest date on two separate runs: that is the edge of what
            # Angel will give, not a speed limit, so stop asking for it.
            oldest = min(rows) if rows else ""
            if oldest and meta.get("refused_at") == oldest:
                meta["exhausted"] = True
                notes.append(f"Angel One will not give data older than {oldest[:10]}")
            else:
                meta["refused_at"] = oldest
                notes.append(f"stopped going further back ({status[9:]}); run it again later to fetch more")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    if not rows:
        raise AngelError("no history could be downloaded" + (f": {notes[-1]}" if notes else ""))
    return [_bar(rows[k]) for k in sorted(rows)], "; ".join(notes)
