"""Backtest report: the exact live rule replayed on every past session.

What is real and what is estimated:
- Index outcomes (did the stop or the target come first, or neither by 15:00) come from real
  Angel One 5-minute index history. That is the honest "success rate" of the rule itself.
- Option P&L after charges is an ESTIMATE. Angel One does not provide prices for expired option
  contracts, so premiums are rebuilt with Black-Scholes using India VIX at the signal time as the
  volatility, today's lot size and today's charges. Real fills, the volatility smile and expiry-day
  moves will differ.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import odds
from .candles import Bar
from .config import Settings
from .timeutil import IST

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SPLIT = date(2025, 1, 1)  # the Nov 2026 rule changes were chosen on data before this date only
_OLD = {"breakout_buffer": 0.0, "exit_mode": "target", "target_r": 2.0, "stop_rule": "mid", "trend_filter": "none"}
_V8 = {"breakout_buffer": 0.2, "exit_mode": "hold", "stop_rule": "mid", "trend_filter": "none"}
VARIANTS = [
    ("original: any close, 2R target", _OLD),
    ("breakout 0.2, hold (v8)", _V8),
    ("A: + yesterday filter, hold", {**_V8, "trend_filter": "yesterday"}),
    ("B: + yesterday, half at 1R", {**_V8, "trend_filter": "yesterday", "exit_mode": "half", "half_at_r": 1.0}),
    ("A with breakout 0.1", {**_V8, "trend_filter": "yesterday", "breakout_buffer": 0.1}),
    ("A with breakout 0.3", {**_V8, "trend_filter": "yesterday", "breakout_buffer": 0.3}),
    ("A with far stop", {**_V8, "trend_filter": "yesterday", "stop_rule": "far"}),
    ("A with trailing exit", {**_V8, "trend_filter": "yesterday", "exit_mode": "trail"}),
    ("A, far stop, trailing exit", {**_V8, "trend_filter": "yesterday", "stop_rule": "far", "exit_mode": "trail"}),
]


def _stats(trades: list[dict], max_loss: float) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    r = [t["r_after_costs"] for t in trades]
    wins = [x for x in r if x > 0]
    losses = [x for x in r if x <= 0]
    cum = peak = dd = 0.0
    streak = worst = 0
    for x in r:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "n": n,
        "target_pct": 100.0 * sum(1 for t in trades if t["outcome"] == "target") / n,
        "stop_pct": 100.0 * sum(1 for t in trades if t["outcome"] == "stop") / n,
        "neither_pct": 100.0 * sum(1 for t in trades if t["outcome"] == "neither") / n,
        "half_pct": 100.0 * sum(1 for t in trades if t["outcome"].startswith("half")) / n,
        "dir_pct": 100.0 * sum(1 for t in trades if t["r_index"] > 0) / n,
        "win_pct": 100.0 * len(wins) / n,
        "avg_r_index": sum(t["r_index"] for t in trades) / n,
        "avg_r": sum(r) / n,
        "per_trade_rs": sum(r) / n * max_loss,
        "total_r": sum(r),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "max_dd_r": dd,
        "worst_streak": worst,
        "best_r": max(r),
        "worst_r": min(r),
    }


def _bucket_dte(t: dict) -> str:
    dte = (date.fromisoformat(t["expiry"]) - date.fromisoformat(t["date"])).days
    return "1 day" if dte <= 1 else ("2-3 days" if dte <= 3 else "4+ days")


def run(index: str, bars: list[Bar], vix: list[Bar], s: Settings, lot: int, step: float, exch: str,
        today: date) -> dict:
    sessions = {d: b for d, b in odds.full_sessions(bars).items() if d < today}
    seen = {b.start.astimezone(IST).date() for b in bars}
    skipped = sorted(d for d in seen if d < today and d not in sessions)
    vix_by: dict[date, list[Bar]] = defaultdict(list)
    for b in vix:
        vix_by[b.start.date()].append(b)
    ctx = odds.Ctx(index, lot, step, exch)
    prev = odds.yesterday_sides(bars)
    main = odds.backtest(sessions, s, ctx, vix_by, keep=None, prev=prev)
    trades = main["trades"]
    out = {"index": index, "summary": main["summary"], "trades": trades, "skipped": skipped,
           "skipped_why": odds.describe_day(bars, skipped[-1]) if skipped else "",
           "overall": _stats(trades, s.max_loss_per_trade), "variants": [], "by_year": {}, "by_side": {},
           "by_weekday": {}, "by_dte": {}}
    groups = {"by_year": lambda t: t["date"][:4], "by_side": lambda t: t["side"],
              "by_weekday": lambda t: WEEKDAYS[date.fromisoformat(t["date"]).weekday()],
              "by_dte": _bucket_dte}
    for name, keyf in groups.items():
        g: dict[str, list] = defaultdict(list)
        for t in trades:
            g[keyf(t)].append(t)
        out[name] = {k: _stats(v, s.max_loss_per_trade) for k, v in sorted(g.items())}
    by_label = {}
    for label, kw in VARIANTS:
        v = replace(s, **kw)
        bt = odds.backtest(sessions, v, ctx, vix_by, keep=None, prev=prev)
        by_label[label] = bt["trades"]
        st = _stats(bt["trades"], s.max_loss_per_trade)
        st.update({"label": label, "current": all(getattr(s, k) == x for k, x in kw.items())})
        out["variants"].append(st)
    # Original rule vs versions A and B, split at the date the changes were chosen on.
    rows = [("original", by_label["original: any close, 2R target"]),
            ("A: hold", by_label["A: + yesterday filter, hold"]),
            ("B: half 1R", by_label["B: + yesterday, half at 1R"])]
    out["periods"] = []
    for name, lo, hi in (("2022-2024 (used to choose)", date.min, SPLIT), ("2025-2026 (not used)", SPLIT, date.max)):
        out["periods"].append({"name": name, "rows": [
            (tag, _stats([t for t in tr if lo <= date.fromisoformat(t["date"]) < hi], s.max_loss_per_trade))
            for tag, tr in rows]})
    r = sorted((t["r_after_costs"] for t in trades), reverse=True)
    out["top10"] = (sum(r[:10]), sum(r)) if len(r) >= 30 else None
    return out


def _pct(x):
    return f"{x:5.1f}%"


def _r(x):
    return f"{x:+.2f}R"


def format_report(results: list[dict], s: Settings) -> str:
    L = []
    w = L.append
    w("=" * 78)
    w("BACKTEST: opening-range breakout, buying the option (signals-only rule)")
    clear = f" by {s.breakout_buffer:g}x its width" if s.breakout_buffer else ""
    exit_txt = {"hold": "no fixed target: exit at the stop or",
                "trail": "no fixed target; after +1R the stop trails 1R behind the best price; exit at the stop or",
                "half": f"sell half at +{s.half_at_r:g}R and move the rest's stop to entry; rest exits at the stop or",
                }.get(s.exit_mode, f"target {s.target_r:g}R,")
    w(f"Rule: range 09:15-09:30 IST; first 5-min close beyond it{clear};")
    if s.trend_filter == "yesterday":
        w("only in the direction of yesterday's move (yesterday up: CE only; down: PE only);")
    w(f"no signals after {s.last_signal_time};")
    w(f"stop at the range {'midpoint' if s.stop_rule == 'mid' else 'far side'}; {exit_txt} time exit {s.time_exit}.")
    w(f"Max loss per trade Rs {s.max_loss_per_trade:,.0f}.")
    w("=" * 78)
    for res in results:
        sm, o = res["summary"], res["overall"]
        w("")
        w(f"{res['index']}  ·  {sm['sessions']} full sessions, {sm['first']} to {sm['last']}")
        w("-" * 78)
        sk = res.get("skipped") or []
        if sk:
            w(f"  Skipped {len(sk)} day(s) without every bar from 09:15 to {odds.LAST_BAR} (short special sessions"
              f" or data gaps); latest {sk[-1].isoformat()}: {res.get('skipped_why', '')}")
        if not o.get("n"):
            w("  No signals in this history.")
            continue
        none_why = "no breakout, or only against yesterday's direction" if s.trend_filter == "yesterday" else "no breakout"
        w(f"  Signals: {o['n']} ({sm['ce']} CE, {sm['pe']} PE); no signal on {sm['no_signal']} days ({none_why})")
        if s.exit_mode in ("hold", "half", "trail"):
            w(f"  On the index (real data):  stopped out {_pct(o['stop_pct'])} · otherwise held"
              + (f" (half sold at +{s.half_at_r:g}R in {_pct(o.get('half_pct', 0))})" if s.exit_mode == "half" else "")
              + f" · win rate after charges {_pct(o['win_pct'])} · direction right {_pct(o['dir_pct'])}")
        else:
            w(f"  On the index (real data):  target first {_pct(o['target_pct'])} · stop first {_pct(o['stop_pct'])}"
              f" · neither by {s.time_exit} {_pct(o['neither_pct'])}")
        w(f"  Average result on the index: {_r(o['avg_r_index'])} per trade")
        w(f"  Option trade after charges (ESTIMATE): profitable {_pct(o['win_pct'])} of trades, "
          f"average {_r(o['avg_r'])} per trade")
        w(f"     = about Rs {o['per_trade_rs']:,.0f} per trade at your max loss; total {o['total_r']:+.1f}R over "
          f"{o['n']} trades")
        pf = "inf" if o["profit_factor"] == float("inf") else f"{o['profit_factor']:.2f}"
        w(f"     profit factor {pf} · worst run of losers {o['worst_streak']} · deepest drawdown "
          f"{o['max_dd_r']:.1f}R · best {_r(o['best_r'])} · worst {_r(o['worst_r'])}")
        w(f"  Verdict: {sm['verdict']}")
        if res.get("top10"):
            t10, tot = res["top10"]
            w(f"  Best 10 trades: {t10:+.1f}R of the {tot:+.1f}R total. "
              + ("Most of the profit comes from a few big trend days, so every signal has to be taken and held."
                 if tot > 0 and t10 > 0.5 * tot else ""))
        w("")
        w("  Original rule vs A and B             signals   avg R   win rate  direction   PF    total")
        for per in res.get("periods", []):
            for j, (tag, st) in enumerate(per["rows"]):
                if not st.get("n"):
                    continue
                pf = "inf" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
                w(f"    {per['name'] if j == 0 else '':<27}{tag:<11}{st['n']:>5}  {_r(st['avg_r']):>7}"
                  f"   {_pct(st['win_pct'])}   {_pct(st['dir_pct'])}  {pf:>5}  {st['total_r']:+7.1f}R")
        w("")
        w("  Variants (same history)            signals  profitable  avg R    PF    total")
        for v in res["variants"]:
            if not v.get("n"):
                continue
            mark = " <- your setting" if v.get("current") else ""
            pf = "inf" if v["profit_factor"] == float("inf") else f"{v['profit_factor']:.2f}"
            w(f"    {v['label']:<33}{v['n']:>6}  {_pct(v['win_pct'])}   {_r(v['avg_r']):>7}  {pf:>5}"
              f"  {v['total_r']:+7.1f}R{mark}")
        for title, key in (("By year", "by_year"), ("By side", "by_side"), ("By weekday", "by_weekday"),
                           ("By days to expiry", "by_dte")):
            w("")
            hold = s.exit_mode in ("hold", "half", "trail")
            w(f"  {title:<22}signals  {'stopped' if hold else ' target'}  profitable  avg R")
            for k, v in res[key].items():
                w(f"    {k:<20}{v['n']:>6}  {_pct(v['stop_pct'] if hold else v['target_pct'])}    "
                  f"{_pct(v['win_pct'])}   {_r(v['avg_r']):>7}")
    w("")
    w("How to read this")
    w("  - 'avg R' is the average result per trade in units of your planned loss: -1R is a full stop-out,")
    w("    +0.10R on a Rs 2,000 max loss is about Rs 200 a trade on average.")
    w("  - 'win rate' = trade made money after charges; 'direction right' = the index moved your way by the exit.")
    w("  - The Nov 2026 changes (stronger breakout close, no fixed target, yesterday's direction) were chosen")
    w("    on 2022-2024 only. 2025-2026 was kept aside as an honest check; that row is the one to believe.")
    w("  - Version B (half at 1R) needs at least 2 lots to split in real trading; here it uses half quantities.")
    w("  - 'Profitable' and 'avg R' are for the option trade after brokerage, STT, exchange charges, GST,")
    w("    stamp duty and slippage. They are ESTIMATES: Angel One has no prices for expired options, so")
    w("    premiums are rebuilt with Black-Scholes and India VIX, with today's lot sizes and charges.")
    w("  - The live app also blocks wide spreads, out-of-range premiums and chased entries; this test")
    w("    cannot, so live results will differ. Past results do not predict future ones.")
    return "\n".join(L)


def write_csv(results: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["index", "date", "side", "time", "entry", "stop", "target", "outcome", "exit", "r_index",
            "iv_source", "strike", "expiry", "premium", "exit_premium", "lots", "pnl", "r_after_costs"]
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for res in results:
            for t in res["trades"]:
                wr.writerow([res["index"]] + [t.get(c) for c in cols[1:]])
