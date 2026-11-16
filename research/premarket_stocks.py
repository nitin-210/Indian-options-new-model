"""Can the pre-market (09:00-09:15) picture pick the best stocks to trade, instead of the first breaks of the day?

What the pre-open tells us is in the opening print: NSE's pre-open call auction (09:00-09:08) sets each stock's opening
price at 09:15 and its matched quantity is traded at the open. Angel's history has no separate pre-open order book,
so the testable pre-market facts are: the gap (open vs yesterday's close) in ATR, the gap against NIFTY's gap
(stock-specific news), where it opened vs yesterday's range (above the high / below the low / inside), the opening
candle's volume vs normal (the auction quantity plus the first 5 minutes), and yesterday's close, move and trend.

At 09:15 (09:20 for the opening volume) each day: rank the 55 stocks by a pre-market score, keep the top N, and trade
only their range break (the same rule: 5-min close beyond the 09:15-09:30 range by 0.2x width, far stop, 15:00 exit),
either side or only in the gap's direction. Scores chosen on the first 60% of days, checked once on the last 40%.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/v2")
from brk_events import D, load  # noqa: E402

nifty = load(D / "NIFTY_5m.json")
ev = pd.read_pickle("/home/claude/ml/v2/brk_events.pkl")[["name", "date", "side", "R", "mfe", "how", "slot"]]
rows = []
for p in sorted((D / "stocks5m").glob("*_5m.json")):
    name = p.stem.replace("_5m", "")
    days = load(p)
    ds = sorted(days)
    for i in range(21, len(ds)):
        d = ds[i]
        a, y = days[d], days[ds[i - 1]]
        prev = [days[x] for x in ds[i - 20:i]]
        atr = np.mean([q[:, 1].max() - q[:, 2].min() for q in prev[-14:]])
        if atr <= 0:
            continue
        o0, yc, yh, yl, yo = a[0, 0], y[-1, 3], y[:, 1].max(), y[:, 2].min(), y[0, 0]
        nd, ny = nifty.get(d), nifty.get(ds[i - 1])
        ngap = (nd[0, 0] / ny[-1, 3] - 1) * 100 if nd is not None and ny is not None else np.nan
        c5 = days[ds[i - 5]][-1, 3]
        rows.append({
            "name": name, "date": d,
            "gap_atr": (o0 - yc) / atr, "gap_pct": (o0 / yc - 1) * 100, "nifty_gap": ngap,
            "rel_gap": (o0 / yc - 1) * 100 - ngap,
            "open_pos": 1 if o0 > yh else (-1 if o0 < yl else 0),
            "vol0": a[0, 4] / max(np.median([q[0, 4] for q in prev]), 1),
            "y_clv": (yc - yl) / (yh - yl) if yh > yl else 0.5,
            "y_ret_atr": (yc - yo) / atr, "y_ra": (yh - yl) / atr, "mom5_atr": (yc - c5) / atr,
            "turn": np.mean([(q[:, 3] * q[:, 4]).sum() for q in prev]) / 1e7,
            # outcome of the whole day, to check "in play"
            "day_range_atr": (a[:, 1].max() - a[:, 2].min()) / atr,
            "trend_atr": abs(a[56 + 12, 3] - o0) / atr,
        })
pm = pd.DataFrame(rows).merge(ev, on=["name", "date"], how="left")
pm.to_pickle("/home/claude/ml/v2/premarket_stocks.pkl")
print(pm.shape, pm.date.min(), pm.date.max())
