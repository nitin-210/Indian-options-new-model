"""Top bullish and bearish F&O stocks, ranked live from the market feed.

Ranking: % change of the stock's cash price from yesterday's close.
Build-up: the near-month future's price change together with its open-interest change:
  price up + OI up = Long build-up (bullish)      price up + OI down = Short covering (bullish, weaker)
  price down + OI up = Short build-up (bearish)   price down + OI down = Long unwinding (bearish, weaker)

This is a list of movers, not signals. Angel One also has a gainers/losers endpoint,
but users report errors and wrong data from it, so the ranking is computed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .instruments import Contract, OptionUniverse
from .strategy import Quote


@dataclass
class FnoStock:
    name: str
    eq_token: str
    fut_token: str
    fut_symbol: str
    fut_expiry: date
    lot: int


@dataclass
class StockState:
    info: FnoStock
    ltp: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    vol: float = 0.0
    last: datetime | None = None
    fut: Quote = field(default_factory=Quote)

    @property
    def pct(self) -> float | None:
        if self.ltp > 0 and self.prev_close > 0:
            return (self.ltp / self.prev_close - 1.0) * 100.0
        return None


def buildup(price_chg: float | None, oi_chg: float | None) -> tuple[str, str]:
    """(label, tone) where tone is bull / bear / ''."""
    if price_chg is None or oi_chg is None or price_chg == 0 or oi_chg == 0:
        return "—", ""
    if price_chg > 0:
        return ("Long build-up", "bull") if oi_chg > 0 else ("Short covering", "bull")
    return ("Short build-up", "bear") if oi_chg > 0 else ("Long unwinding", "bear")


def parse_fno_stocks(rows: list[dict], today: date) -> tuple[list[FnoStock], list[Contract]]:
    """F&O stocks from Angel's instrument file: cash token, nearest future, lot size, stock options."""
    from .instruments import parse_expiry

    eq: dict[str, str] = {}
    futs: dict[str, dict] = {}
    opt_rows: list[dict] = []
    for r in rows:
        ex, it, sym = r.get("exch_seg"), r.get("instrumenttype"), r.get("symbol", "")
        if "NSETEST" in (r.get("name") or "") or "NSETEST" in sym:
            continue                     # NSE's dummy test securities in Angel's list: not real stocks
        if ex == "NSE" and sym.endswith("-EQ"):
            eq[r.get("name") or sym[:-3]] = str(r["token"])
        elif ex == "NFO" and it == "FUTSTK":
            try:
                e = parse_expiry(r["expiry"])
            except (KeyError, ValueError):
                continue
            if e < today:
                continue
            cur = futs.get(r["name"])
            if cur is None or e < cur["_exp"]:
                futs[r["name"]] = dict(r, _exp=e)
        elif ex == "NFO" and it == "OPTSTK":
            opt_rows.append(r)
    stocks = []
    for name, f in sorted(futs.items()):
        if name in eq:
            stocks.append(FnoStock(name, eq[name], str(f["token"]), f["symbol"], f["_exp"],
                                   int(float(f["lotsize"]))))
    names = {s.name for s in stocks}
    opts: list[Contract] = []
    near: dict[str, list[date]] = {}
    for r in opt_rows:
        if r.get("name") not in names:
            continue
        try:
            e = parse_expiry(r["expiry"])
        except (KeyError, ValueError):
            continue
        if e < today:
            continue
        near.setdefault(r["name"], []).append(e)
    keep = {n: set(sorted(set(v))[:2]) for n, v in near.items()}  # nearest two monthly expiries
    for r in opt_rows:
        n = r.get("name")
        if n not in keep:
            continue
        try:
            e = parse_expiry(r["expiry"])
            if e not in keep[n] or r.get("symbol", "")[-2:] not in ("CE", "PE"):
                continue
            opts.append(Contract(str(r["token"]), r["symbol"], n, e, round(float(r["strike"]) / 100.0, 2),
                                 r["symbol"][-2:], int(float(r["lotsize"])),
                                 round(float(r.get("tick_size", 5)) / 100.0, 4) or 0.05, "NFO"))
        except (KeyError, ValueError):
            continue
    return stocks, opts


class Movers:
    def __init__(self):
        self.stocks: dict[str, StockState] = {}
        self.by_eq: dict[str, str] = {}
        self.by_fut: dict[str, str] = {}
        self.option_contracts: dict[str, Contract] = {}
        self.universes: dict[str, OptionUniverse] = {}
        self.selected: str | None = None
        self.selected_tokens: set[str] = set()   # ATM +-2 strikes, calls and puts (live quotes)
        self.atm_tokens: set[str] = set()        # the ATM call and put shown as cards
        self.selected_atm: float | None = None
        self.label = ""

    def load(self, stocks: list[FnoStock], options: list[Contract], label: str = "") -> None:
        self.stocks = {s.name: StockState(s) for s in stocks}
        self.by_eq = {s.eq_token: s.name for s in stocks}
        self.by_fut = {s.fut_token: s.name for s in stocks}
        self.option_contracts = {c.token: c for c in options}
        by_name: dict[str, list[Contract]] = {}
        for c in options:
            by_name.setdefault(c.name, []).append(c)
        self.universes = {n: OptionUniverse(n, cs) for n, cs in by_name.items()}
        self.label = label

    def tokens(self) -> dict[int, set[str]]:
        return {1: set(self.by_eq), 2: set(self.by_fut)}

    def on_tick(self, t: dict, now: datetime, prev_oi: dict[str, int]) -> bool:
        tok, wt = t.get("token"), t.get("ws_type")
        if wt == 1 and tok in self.by_eq:
            st = self.stocks[self.by_eq[tok]]
            if t.get("ltp"):
                st.ltp = t["ltp"]
            for k in ("open", "high", "low"):
                if t.get(k):
                    setattr(st, k, t[k])
            if t.get("close"):
                st.prev_close = t["close"]
            if "vol" in t:
                st.vol = float(t["vol"] or 0)
            st.last = now
            return True
        if wt == 2 and tok in self.by_fut:
            q = self.stocks[self.by_fut[tok]].fut
            if t.get("ltp"):
                q.ltp = t["ltp"]
            if t.get("close"):
                q.prev_close = t["close"]
            if "oi" in t:
                q.oi = int(t["oi"] or 0)
                q.oi_chg_pct = t.get("oi_chg_pct")
            if tok in prev_oi:
                q.prev_oi = prev_oi[tok]
            return True
        return False

    def select(self, name: str, today: date) -> set[str]:
        """The nearest monthly expiry's strikes around the money (ATM +-2, calls and puts) for one stock."""
        st = self.stocks.get(name)
        u = self.universes.get(name)
        self.selected = name if st else None
        self.selected_tokens, self.atm_tokens, self.selected_atm = set(), set(), None
        if not st or not u or st.ltp <= 0:
            return self.selected_tokens
        exp, _ = u.choose_expiry(today)
        if exp is None:
            return self.selected_tokens
        ks = u.strikes(exp)
        k = u.atm(st.ltp, exp)
        if k not in ks:
            return self.selected_tokens
        self.selected_atm = k
        i = ks.index(k)
        for j in range(max(0, i - 2), min(len(ks), i + 3)):
            for opt in ("CE", "PE"):
                c = u.contract(exp, ks[j], opt)
                if c:
                    self.selected_tokens.add(c.token)
                    if ks[j] == k:
                        self.atm_tokens.add(c.token)
        return self.selected_tokens

    def rows(self) -> list[dict]:
        out = []
        for st in self.stocks.values():
            pct = st.pct
            if pct is None:
                continue
            f = st.fut
            fpct = (f.ltp / f.prev_close - 1) * 100 if f.ltp > 0 and f.prev_close > 0 else None
            oi_chg = f.oi_change
            label, tone = buildup(fpct, oi_chg)
            out.append({"name": st.info.name, "ltp": round(st.ltp, 2), "pct": round(pct, 2),
                        "turnover_cr": round(st.ltp * st.vol / 1e7, 1) if st.vol else None,
                        "lot": st.info.lot, "oi_chg_pct": round(oi_chg / (f.oi - oi_chg) * 100, 1)
                        if oi_chg and f.oi - oi_chg else None,
                        "buildup": label, "tone": tone})
        return out

    def snapshot(self, n: int, detail: dict | None) -> dict:
        rows = self.rows()
        ups = sorted((r for r in rows if r["pct"] > 0), key=lambda r: -r["pct"])[:n]
        downs = sorted((r for r in rows if r["pct"] < 0), key=lambda r: r["pct"])[:n]
        return {"bull": ups, "bear": downs, "tracked": len(self.stocks), "priced": len(rows),
                "label": self.label, "selected": self.selected, "detail": detail}
