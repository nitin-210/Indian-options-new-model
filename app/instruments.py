"""Angel One instrument master: option contracts, expiries, strikes, lot sizes.

Nothing here is hard-coded per contract: lot size, tick size, strike step and
expiry dates all come from the instrument file Angel publishes every day.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Index metadata. ws_type is the SmartAPI WebSocket exchange type.
# step / lot are fallbacks only (the live values come from Angel's instrument file).
# tested = part of the backtested system (NIFTY, SENSEX); NIFTY BANK uses the same rule but was not backtested.
INDEX_META = {
    "NIFTY": {"label": "NIFTY 50", "exchange": "NSE", "ws_type": 1, "token": "99926000",
              "opt_exchange": "NFO", "opt_ws_type": 2, "txn": "nse", "step": 50.0, "lot": 65, "tested": True},
    "SENSEX": {"label": "SENSEX", "exchange": "BSE", "ws_type": 3, "token": "99919000",
               "opt_exchange": "BFO", "opt_ws_type": 4, "txn": "bse", "step": 100.0, "lot": 20, "tested": True},
    "BANKNIFTY": {"label": "NIFTY BANK", "exchange": "NSE", "ws_type": 1, "token": "99926009",
                  "opt_exchange": "NFO", "opt_ws_type": 2, "txn": "nse", "step": 100.0, "lot": 30, "tested": False},
}
VIX_META = {"label": "India VIX", "exchange": "NSE", "ws_type": 1, "token": "99926017"}


@dataclass(frozen=True)
class Contract:
    token: str
    symbol: str
    name: str  # NIFTY / SENSEX
    expiry: date
    strike: float
    opt: str  # CE / PE
    lot: int
    tick: float
    exchange: str  # NFO / BFO

    @property
    def ws_type(self) -> int:
        return 2 if self.exchange == "NFO" else 4

    @property
    def expiry_code(self) -> str:
        """Angel's expiry format, e.g. 17NOV2026 (used by the Greeks API)."""
        return self.expiry.strftime("%d%b%Y").upper()

    def label(self) -> str:
        return f"{self.name} {self.strike:,.0f} {self.opt} ({self.expiry.day} {self.expiry.strftime('%b')})"

    def to_json(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Contract":
        d = dict(d)
        d["expiry"] = date.fromisoformat(d["expiry"])
        return cls(**d)


def parse_expiry(s: str) -> date:
    return datetime.strptime(s.strip().upper(), "%d%b%Y").date()


def parse_master(rows: list[dict], names=tuple(INDEX_META)) -> list[Contract]:
    """Keep index options (OPTIDX) for the given underlyings.

    In Angel's file the strike and tick size are in paise (x100):
    strike "2560000.000000" means 25,600 and tick "5.000000" means 0.05.
    """
    out: list[Contract] = []
    for r in rows:
        if r.get("instrumenttype") != "OPTIDX":
            continue
        name = r.get("name", "")
        if name not in names:
            continue
        exch = r.get("exch_seg", "")
        if exch != INDEX_META[name]["opt_exchange"]:
            continue
        sym = r.get("symbol", "")
        opt = sym[-2:]
        if opt not in ("CE", "PE"):
            continue
        try:
            out.append(Contract(
                token=str(r["token"]),
                symbol=sym,
                name=name,
                expiry=parse_expiry(r["expiry"]),
                strike=round(float(r["strike"]) / 100.0, 2),
                opt=opt,
                lot=int(float(r["lotsize"])),
                tick=round(float(r.get("tick_size", 5)) / 100.0, 4) or 0.05,
                exchange=exch,
            ))
        except (KeyError, ValueError) as e:
            log.debug("skipping instrument row %s: %s", sym, e)
    return out


class OptionUniverse:
    """Lookup of option contracts for one underlying."""

    def __init__(self, name: str, contracts: list[Contract]):
        self.name = name
        self.contracts = [c for c in contracts if c.name == name]
        self._by_key = {(c.expiry, c.strike, c.opt): c for c in self.contracts}
        self.by_token = {c.token: c for c in self.contracts}
        self.expiries = sorted({c.expiry for c in self.contracts})

    def __bool__(self) -> bool:
        return bool(self.contracts)

    def choose_expiry(self, today: date) -> tuple[date | None, list[str]]:
        """Nearest expiry on or after today; on expiry day use the next one and say so."""
        flags: list[str] = []
        future = [e for e in self.expiries if e >= today]
        if not future:
            return None, ["No live expiries in the instrument file"]
        if future[0] == today:
            flags.append(f"Today is a {self.name} expiry day: using the next expiry "
                         f"({future[1].day} {future[1].strftime('%b')}) instead" if len(future) > 1
                         else f"Today is a {self.name} expiry day and there is no next expiry")
            if len(future) < 2:
                return None, flags
            return future[1], flags
        return future[0], flags

    def is_expiry_day(self, d: date) -> bool:
        return d in self.expiries

    def strikes(self, expiry: date) -> list[float]:
        return sorted({c.strike for c in self.contracts if c.expiry == expiry})

    def step(self, expiry: date) -> float:
        ks = self.strikes(expiry)
        diffs = sorted({round(b - a, 2) for a, b in zip(ks, ks[1:]) if b > a})
        if not diffs:
            return float(INDEX_META.get(self.name, {}).get("step", 100.0))
        # The most common gap near the money; use the smallest common gap.
        counts: dict[float, int] = {}
        for a, b in zip(ks, ks[1:]):
            g = round(b - a, 2)
            counts[g] = counts.get(g, 0) + 1
        return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    def atm(self, spot: float, expiry: date) -> float:
        ks = self.strikes(expiry)
        if not ks:
            st = self.step(expiry)
            return round(spot / st) * st
        return min(ks, key=lambda k: (abs(k - spot), k))

    def chain(self, center: float, expiry: date, each_side: int) -> list[float]:
        ks = self.strikes(expiry)
        if not ks:
            return []
        i = ks.index(min(ks, key=lambda k: abs(k - center)))
        return ks[max(0, i - each_side): i + each_side + 1]

    def contract(self, expiry: date, strike: float, opt: str) -> Contract | None:
        return self._by_key.get((expiry, round(strike, 2), opt))

    def pick_strike(self, spot: float, expiry: date, side: str, mode: str) -> float:
        """ATM, or one strike in the money (lower strike for a CE, higher for a PE)."""
        atm = self.atm(spot, expiry)
        if mode != "itm1":
            return atm
        ks = self.strikes(expiry)
        i = ks.index(atm)
        if side == "CE":
            return ks[i - 1] if i > 0 else atm
        return ks[i + 1] if i + 1 < len(ks) else atm


def save_contracts(contracts: list[Contract], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([c.to_json() for c in contracts]), encoding="utf-8")


def load_contracts(path: Path) -> list[Contract]:
    return [Contract.from_json(d) for d in json.loads(path.read_text(encoding="utf-8"))]
