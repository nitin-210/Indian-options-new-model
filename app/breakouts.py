"""Range breaks: every F&O stock whose 5-minute candle closed beyond its first-15-minute range, and the ones that
were top movers when they broke.

The break is the same rule as the picks' "broke up / broke down" (and the index calls): the range is 09:15-09:30, and
the break is the first completed 5-minute candle after 09:30 that closes beyond the range by 0.2x its width. Each
stock is checked once per closed candle, so a scan over ~200 stocks stays cheap even in a fast replay.

Top movers' breaks: a breakout by a stock that was among the day's top 10 gainers (% from yesterday's close) when its
break candle closed, or a breakdown by one of the top 10 losers - the same ranking as the Top movers panel.

After the break the list keeps score: "holding" (still beyond the trigger), "back inside" (back within the range or
between the range and the trigger), or "reversed" (a later candle reached the far side of the range, the stop the
picks use). Breaks after 14:00 are listed but marked late: the tested window for signals ends at 14:00.

Each break is also followed as a trade (the dashboard's short lists): entry at the break candle's close, stop at the
far side of the range, target 2R; it ends at the first of: the stop touched, the target touched, a 5-minute candle
closing back inside the range, or 15:00. research: /home/claude/ml/stocklist/sim.py.

Tested on 55 large-cap F&O stocks, 5-minute candles, Dec 2022 - Nov 2026 (40,120 breaks; first 60% of days to
choose, last 40% to check; research/topmover_brk.py): every break about +0.00R a trade before option costs; top
movers' breaks about -0.01R (stopped less often, about 10% vs 17%, but no further moves). Option costs are about
0.10R a trade, so neither is a profitable signal on its own.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .timeutil import IST, hm, ist

BUFFER = 0.2          # x the range width, as the tested rule
LAST_SIGNAL = "14:00"
TOP_N = 10            # top gainers / losers, as the Top movers panel
TARGET_R = 2.0        # the short lists' target: 2x the risk (entry to the far side of the range)


class BreakoutScanner:
    def __init__(self, engine):
        self.e = engine
        self.day: date | None = None
        self.state: dict[str, dict] = {}
        self._pct_at: dict[datetime, dict[str, float]] = {}

    def _pcts(self, today: date, bar_start: datetime) -> dict[str, float]:
        """Every stock's % change from yesterday's close at the close of the 5-minute candle starting bar_start."""
        if bar_start not in self._pct_at:
            out = {}
            for name, st in self.e.movers.stocks.items():
                c1, c5 = self.e.stock_bars.get(name, (None, None))
                if c5 is None or st.prev_close <= 0:
                    continue
                last = None
                for b in c5.bars:
                    if b.start.astimezone(IST).date() == today and b.start <= bar_start:
                        last = b
                if last is not None:
                    out[name] = (last.c / st.prev_close - 1) * 100
            self._pct_at[bar_start] = out
        return self._pct_at[bar_start]

    @staticmethod
    def _rank(pcts: dict[str, float], name: str, sd: int) -> int | None:
        """1 = the biggest gainer (sd 1) / loser (sd -1); None if it is not moving that way at all."""
        me = pcts.get(name)
        if me is None or me * sd <= 0:
            return None
        return 1 + sum(1 for v in pcts.values() if v * sd > me * sd)

    def scan(self, now: datetime, picks: list[dict] | None = None, ranks: dict[str, float] | None = None) -> list[dict]:
        e = self.e
        now = now.astimezone(IST)
        today = now.date()
        if self.day != today:
            self.day, self.state, self._pct_at = today, {}, {}
        if now < ist(today, hm("09:30")):
            return []
        t930, t_last = ist(today, hm("09:30")), ist(today, hm(LAST_SIGNAL))
        t1455 = ist(today, hm("14:55"))
        in_picks = {p["name"] for p in (picks or [])}
        live = [(n, s.pct) for n, s in e.movers.stocks.items() if s.pct is not None and s.ltp > 0]
        gain = sorted((x for x in live if x[1] > 0), key=lambda x: -x[1])
        lose = sorted((x for x in live if x[1] < 0), key=lambda x: x[1])
        now_rank = {**{("CE", n): i + 1 for i, (n, _) in enumerate(gain)},
                    **{("PE", n): i + 1 for i, (n, _) in enumerate(lose)}}
        out = []
        for name, st in e.movers.stocks.items():
            c1, c5 = e.stock_bars.get(name, (None, None))
            if c5 is None or c1 is None:
                continue
            fives = [b for b in c5.bars if b.start.astimezone(IST).date() == today]
            s = self.state.get(name)
            if s is None or len(fives) < s["n"] or (fives and s["first"] != fives[0].start):
                first = [b for b in c1.bars if b.start.astimezone(IST).date() == today and b.start < t930]
                if not first or first[0].start.astimezone(IST).strftime("%H:%M") > "09:17" or c1.bars[-1].start < t930:
                    continue                      # range unknown (no candles from the open) or not complete yet
                h, lo = max(b.h for b in first), min(b.l for b in first)
                w = h - lo
                s = {"n": 0, "first": fives[0].start if fives else None, "h": h, "lo": lo,
                     "up": h + BUFFER * w, "dn": lo - BUFFER * w, "brk": None, "best": 0.0, "stop": None, "exit": None}
                self.state[name] = s
            for i in range(s["n"], len(fives)):
                b = fives[i]
                if not b.closed:
                    break
                s["n"] = i + 1
                if b.start < t930:
                    continue
                end = b.start + timedelta(minutes=5)
                if s["brk"] is None:
                    if b.c > s["up"]:
                        s["brk"] = {"side": "CE", "end": end, "close": b.c, "bar": b.start}
                    elif b.c < s["dn"]:
                        s["brk"] = {"side": "PE", "end": end, "close": b.c, "bar": b.start}
                    continue
                if s["exit"] is None:
                    s["exit"] = _exit(s, b, end, t1455)
                if s["stop"] is None:
                    ce = s["brk"]["side"] == "CE"
                    if (b.l <= s["lo"]) if ce else (b.h >= s["h"]):
                        s["stop"] = end
                    else:
                        risk = abs(s["brk"]["close"] - (s["lo"] if ce else s["h"]))
                        fav = ((b.h - s["brk"]["close"]) if ce else (s["brk"]["close"] - b.l)) / risk if risk else 0.0
                        s["best"] = max(s["best"], fav)
            if s["brk"] is None or st.ltp <= 0:
                continue
            k = s["brk"]
            ce = k["side"] == "CE"
            sd = 1 if ce else -1
            entry, stop = k["close"], (s["lo"] if ce else s["h"])
            risk = abs(entry - stop)
            if s["stop"] is not None:
                status = "reversed"
            elif (st.ltp > s["up"]) if ce else (st.ltp < s["dn"]):
                status = "holding"
            else:
                status = "back inside"
            if "mrank" not in k:          # its place among the movers when the break candle closed
                pc = self._pcts(today, k["bar"])
                k["mrank"], k["mpct"] = self._rank(pc, name, sd), pc.get(name)
            atr = _atr(e, name, today)
            rank = (ranks or {}).get(name)
            top = k["mrank"] is not None and k["mrank"] <= TOP_N and k["end"] <= t_last
            out.append({
                "name": name, "side": k["side"], "time": k["end"].strftime("%H:%M"), "late": k["end"] > t_last,
                "trigger": round(s["up"] if ce else s["dn"], 2), "close": round(entry, 2),
                "high": round(s["h"], 2), "low": round(s["lo"], 2), "stop": round(stop, 2),
                "ltp": round(st.ltp, 2), "pct": round(st.pct, 2) if st.pct is not None else None,
                "since_pct": round((st.ltp / entry - 1) * 100, 2) if entry else None,
                "r": round((st.ltp - entry) * sd / risk, 2) if risk else None, "best_r": round(s["best"], 2),
                "status": status, "stop_time": s["stop"].strftime("%H:%M") if s["stop"] else None,
                "width_atr": round((s["h"] - s["lo"]) / atr, 2) if atr else None,
                "in_picks": name in in_picks,
                "stars": None if rank is None else (3 if rank >= 0.9 else 2 if rank >= 0.75 else 1),
                "mover_rank": k["mrank"], "mover_pct": round(k["mpct"], 2) if k["mpct"] is not None else None,
                "mover_now": now_rank.get((k["side"], name)), "top_mover": top,
                "target": round(entry + sd * TARGET_R * risk, 2), "risk": round(risk, 2),
                "exit": None if s["exit"] is None else {**s["exit"], "time": s["exit"]["time"].strftime("%H:%M")},
            })
        out.sort(key=lambda x: x["name"])
        out.sort(key=lambda x: x["time"], reverse=True)          # newest first
        return out


def _exit(s: dict, b, end: datetime, t1455: datetime) -> dict | None:
    """Does this closed 5-minute candle end the break's trade? Stop first (the cautious order inside one candle)."""
    k = s["brk"]
    ce = k["side"] == "CE"
    sd = 1 if ce else -1
    entry, stop = k["close"], (s["lo"] if ce else s["h"])
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target = entry + sd * TARGET_R * risk
    if (b.l <= stop) if ce else (b.h >= stop):
        why, px = "stop", (min(b.o, stop) if ce else max(b.o, stop))
    elif (b.h >= target) if ce else (b.l <= target):
        why, px = "target", target
    elif (b.c <= s["h"]) if ce else (b.c >= s["lo"]):
        why, px = "back inside", b.c
    elif b.start >= t1455:
        why, px = "15:00", b.c
    else:
        return None
    return {"why": why, "time": end, "px": round(px, 2), "r": round((px - entry) * sd / risk, 2)}


def _atr(e, name: str, today: date) -> float | None:
    rows = [r for r in (e.stock_daily.get(name) or []) if r[0] < today and e.cal.is_trading_day(r[0])]
    if len(rows) < 14:
        return None
    return sum(r[2] - r[3] for r in rows[-14:]) / 14
