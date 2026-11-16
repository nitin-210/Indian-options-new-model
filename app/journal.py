"""Paper-trading journal (SQLite). Every signal is logged automatically.

A signal that passed the risk check becomes a paper trade: it 'buys' at the
live ask when the signal fires and 'sells' at the live bid when the index
reaches the stop or target, or at 15:00 IST. Signals that were blocked are
tracked too ("shadow"), so you can see what you skipped, but they never count
toward your daily limits.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  idx TEXT NOT NULL,
  side TEXT NOT NULL,
  symbol TEXT NOT NULL,
  token TEXT NOT NULL,
  kind TEXT NOT NULL,          -- paper | shadow
  verdict TEXT NOT NULL,       -- PASS | BLOCK
  reasons TEXT NOT NULL,
  signal_time TEXT NOT NULL,
  entry_index REAL, stop_index REAL, target_index REAL,
  lots INTEGER, qty INTEGER, lot INTEGER,
  entry_fill REAL, planned_loss REAL,
  status TEXT NOT NULL,        -- open | closed
  exit_time TEXT, exit_index REAL, exit_fill REAL, exit_reason TEXT,
  costs REAL, pnl REAL, r_mult REAL,
  mode TEXT NOT NULL,          -- live | demo | replay
  plan_json TEXT
);
CREATE INDEX IF NOT EXISTS trades_day ON trades(day);
CREATE TABLE IF NOT EXISTS paper_trades (      -- trades you place yourself on the paper desk (one click)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL, day TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- index | stock
  name TEXT NOT NULL, side TEXT NOT NULL, symbol TEXT NOT NULL, token TEXT NOT NULL,
  exch TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL, lot INTEGER NOT NULL, lots INTEGER NOT NULL,
  qty INTEGER NOT NULL, tick REAL NOT NULL,
  opened_at TEXT NOT NULL, entry_premium REAL NOT NULL, entry_spot REAL NOT NULL,
  stop_spot REAL, target_spot REAL, rule TEXT NOT NULL, rule_json TEXT,
  status TEXT NOT NULL,                -- open | closed
  closed_at TEXT, exit_premium REAL, exit_spot REAL, exit_reason TEXT,
  costs REAL, pnl REAL, greeks_json TEXT, split_json TEXT
);
CREATE TABLE IF NOT EXISTS paper_account (mode TEXT PRIMARY KEY, capital REAL NOT NULL, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_prefs (mode TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (mode, key));
"""


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(trades)")}
            if "versions_json" not in cols:   # v9: results of exit versions A and B for each trade
                self._db.execute("ALTER TABLE trades ADD COLUMN versions_json TEXT")
            if "strategy" not in cols:        # v14.21: which tested strategy the trade came from
                self._db.execute("ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'orb'")
            # Clean-up for an old bug: before v4, a signal found in the day's history after the fact
            # (app started late) was logged as a blocked trade with stale premiums. Such rows were
            # always blocked for being minutes old, which a signal seen live never is.
            self._db.execute("DELETE FROM trades WHERE kind='shadow' AND reasons LIKE '%min old%'")
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def open_trade(self, *, day: date, plan: dict, verdict: str, reasons: list[str], mode: str,
                   strategy: str = "orb") -> int:
        c = plan["contract"]
        kind = "paper" if verdict == "PASS" else "shadow"
        lots = plan["lots"] if plan["lots"] else 1
        planned = plan["loss_at_stop"] if plan["lots"] else plan["one_lot_loss"]
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO trades (day, idx, side, symbol, token, kind, verdict, reasons, signal_time,
                   entry_index, stop_index, target_index, lots, qty, lot, entry_fill, planned_loss,
                   status, mode, plan_json, strategy)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)""",
                (day.isoformat(), plan["index"], plan["side"], c["symbol"], c["token"], kind, verdict,
                 json.dumps(reasons), plan["signal_time"], plan["entry_index"], plan["stop_index"],
                 plan["target_index"], lots, lots * c["lot"], c["lot"], plan["entry_fill"], planned,
                 mode, json.dumps(plan), strategy))
            self._db.commit()
            return int(cur.lastrowid)

    def close_trade(self, trade_id: int, *, exit_time: datetime, exit_index: float, exit_fill: float,
                    reason: str, costs: float) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if row is None or row["status"] == "closed":
                return dict(row) if row else {}
            pnl = row["qty"] * (exit_fill - row["entry_fill"]) - costs
            r_mult = pnl / row["planned_loss"] if row["planned_loss"] else 0.0
            self._db.execute(
                """UPDATE trades SET status='closed', exit_time=?, exit_index=?, exit_fill=?, exit_reason=?,
                   costs=?, pnl=?, r_mult=? WHERE id=?""",
                (exit_time.isoformat(), exit_index, exit_fill, reason, round(costs, 2), round(pnl, 2),
                 round(r_mult, 3), trade_id))
            self._db.commit()
            return dict(self._db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())

    def set_versions(self, trade_id: int, versions: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE trades SET versions_json=? WHERE id=?", (json.dumps(versions), trade_id))
            self._db.commit()

    def promote(self, trade_id: int, entry_fill: float) -> None:
        """A blocked signal that later passes the risk check becomes a paper trade from now."""
        with self._lock:
            self._db.execute("UPDATE trades SET kind='paper', verdict='PASS', entry_fill=? "
                             "WHERE id=? AND kind='shadow' AND status='open'", (entry_fill, trade_id))
            self._db.commit()

    def delete_day(self, day: date, mode: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM trades WHERE day=? AND mode=?", (day.isoformat(), mode))
            self._db.commit()

    def get(self, trade_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def open_before(self, day: date, mode: str) -> list[dict]:
        """Trades still open from days before `day` (the app was closed before their exit)."""
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM trades WHERE status='open' AND mode=? AND day<? ORDER BY id", (mode, day.isoformat()))]

    # ------------------------------------------------------------ paper desk (your own clicks)
    def paper_capital(self, mode: str, default: float) -> tuple[float, str]:
        with self._lock:
            row = self._db.execute("SELECT capital, since FROM paper_account WHERE mode=?", (mode,)).fetchone()
            if row is None:
                since = datetime.now().isoformat(timespec="seconds")
                self._db.execute("INSERT INTO paper_account (mode, capital, since) VALUES (?,?,?)", (mode, default, since))
                self._db.commit()
                return default, since
            return float(row["capital"]), row["since"]

    def paper_reset(self, mode: str, capital: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM paper_trades WHERE mode=?", (mode,))
            self._db.execute("INSERT OR REPLACE INTO paper_account (mode, capital, since) VALUES (?,?,?)",
                             (mode, capital, datetime.now().isoformat(timespec="seconds")))
            self._db.commit()

    def paper_add(self, row: dict) -> int:
        cols = list(row)
        with self._lock:
            cur = self._db.execute(f"INSERT INTO paper_trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   [row[c] for c in cols])
            self._db.commit()
            return int(cur.lastrowid)

    def paper_close(self, trade_id: int, fields: dict) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if row is None or row["status"] != "open":
                return dict(row) if row else None
            sets = ", ".join(f"{k}=?" for k in fields)
            self._db.execute(f"UPDATE paper_trades SET status='closed', {sets} WHERE id=?", [*fields.values(), trade_id])
            self._db.commit()
            return dict(self._db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone())

    def paper_get(self, trade_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def paper_list(self, mode: str, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM paper_trades WHERE mode=?" + (" AND status=?" if status else "") + " ORDER BY id"
        with self._lock:
            return [dict(r) for r in self._db.execute(q, (mode, status) if status else (mode,))]

    def paper_delete_day(self, day: date, mode: str) -> None:
        """A replay starting again: its OPEN paper trades never happen in the new run; closed ones stay as history."""
        with self._lock:
            self._db.execute("DELETE FROM paper_trades WHERE day=? AND mode=? AND status='open'", (day.isoformat(), mode))
            self._db.commit()

    def paper_pref(self, mode: str, key: str, default: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM paper_prefs WHERE mode=? AND key=?", (mode, key)).fetchone()
        return row[0] if row else default

    def paper_pref_set(self, mode: str, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO paper_prefs (mode, key, value) VALUES (?,?,?)", (mode, key, value))
            self._db.commit()

    def paper_delete_auto_day(self, day: date, mode: str) -> None:
        """a replay starting again re-buys its own 09:15 auto straddles: drop the earlier run's"""
        with self._lock:
            self._db.execute("DELETE FROM paper_trades WHERE day=? AND mode=? AND rule='straddle' AND "
                             "rule_json LIKE '%\"auto\": true%'", (day.isoformat(), mode))
            self._db.commit()

    def paper_delete(self, trade_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM paper_trades WHERE id=?", (trade_id,))
            self._db.commit()

    def find_open(self, day: date, idx: str, mode: str, strategy: str = "orb") -> dict | None:
        """Today's trade of one strategy on one index (open or closed), if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM trades WHERE day=? AND idx=? AND mode=? AND strategy=? ORDER BY id DESC LIMIT 1",
                (day.isoformat(), idx, mode, strategy)).fetchone()
        return dict(row) if row else None

    def today(self, day: date, mode: str) -> dict:
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM trades WHERE day=? AND mode=? AND kind='paper'", (day.isoformat(), mode))]
        realized = sum(r["pnl"] or 0 for r in rows if r["status"] == "closed")
        open_risk = sum(r["planned_loss"] or 0 for r in rows if r["status"] == "open")
        return {"trades": len(rows), "realized": round(realized, 2), "open_risk": round(open_risk, 2)}

    def list(self, mode: str | None = None, limit: int = 500) -> list[dict]:
        q = "SELECT * FROM trades" + (" WHERE mode=?" if mode else "") + " ORDER BY id DESC LIMIT ?"
        args = (mode, limit) if mode else (limit,)
        with self._lock:
            rows = [dict(r) for r in self._db.execute(q, args)]
        for r in rows:
            r.pop("plan_json", None)
            r["reasons"] = json.loads(r["reasons"] or "[]")
            r["versions"] = json.loads(r.get("versions_json") or "null")
            r.pop("versions_json", None)
        return rows

    def stats(self, mode: str | None = None) -> dict:
        rows = list(reversed(self.list(mode, limit=100000)))
        def summarize(sel):
            closed = [r for r in sel if r["status"] == "closed"]
            wins = [r for r in closed if (r["pnl"] or 0) > 0]
            net = sum(r["pnl"] or 0 for r in closed)
            return {"signals": len(sel), "closed": len(closed), "wins": len(wins),
                    "win_rate": round(len(wins) / len(closed), 3) if closed else None,
                    "net_pnl": round(net, 2),
                    "avg_r": round(sum(r["r_mult"] or 0 for r in closed) / len(closed), 3) if closed else None,
                    "targets": sum(1 for r in closed if r["exit_reason"] == "target"),
                    "stops": sum(1 for r in closed if (r["exit_reason"] or "").startswith("stop")),
                    "time_exits": sum(1 for r in closed if r["exit_reason"] == "time")}
        paper = [r for r in rows if r["kind"] == "paper"]
        shadow = [r for r in rows if r["kind"] == "shadow"]
        equity, total = [], 0.0
        by_day: dict[str, float] = {}
        for r in paper:
            if r["status"] == "closed":
                by_day[r["day"]] = by_day.get(r["day"], 0.0) + (r["pnl"] or 0)
        for d in sorted(by_day):
            total += by_day[d]
            equity.append({"day": d, "pnl": round(by_day[d], 2), "cum": round(total, 2)})
        def version(sel, key):
            done = [r["versions"][key] for r in sel if r["status"] == "closed" and r.get("versions")
                    and key in r["versions"] and r["versions"][key].get("pnl") is not None]
            wins = [x for x in done if x["pnl"] > 0]
            return {"closed": len(done), "wins": len(wins),
                    "win_rate": round(len(wins) / len(done), 3) if done else None,
                    "net_pnl": round(sum(x["pnl"] for x in done), 2),
                    "avg_r": round(sum(x.get("r", 0) for x in done) / len(done), 3) if done else None}
        return {"paper": summarize(paper), "shadow": summarize(shadow), "all": summarize(rows),
                "equity": equity, "versions": {"A": version(paper, "A"), "B": version(paper, "B")}}
