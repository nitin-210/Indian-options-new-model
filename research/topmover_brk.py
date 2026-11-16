"""Breaks on top movers only: at the break candle's close, is the stock among the day's top gainers (breakout) or top
losers (breakdown) by % change from yesterday's close? 55 large caps, 5-min, Dec 2022 - Nov 2026, R per trade before
option costs (~0.10R). The app ranks ~200 F&O stocks and shows the top 10 = top 5%; here top 5% of 55 is about 3."""
import sys
import numpy as np, pandas as pd
sys.path.insert(0, "/home/claude/ml/v2")
from brk_events import D, load  # noqa: E402
pct = {}
for p in sorted((D / "stocks5m").glob("*_5m.json")):
    name = p.stem.replace("_5m", ""); days = load(p); ds = sorted(days)
    for i in range(1, len(ds)):
        pct[(name, ds[i])] = days[ds[i]][:, 3] / days[ds[i - 1]][-1, 3] - 1
ev = pd.read_pickle("brk_events.pkl")
by_day = {}
for (n, d), arr in pct.items():
    by_day.setdefault(d, {})[n] = arr
rk, nn, mv = [], [], []
for r in ev.itertuples():
    allp = by_day.get(r.date, {})
    me = allp.get(r.name)
    if me is None:
        rk.append(np.nan); nn.append(np.nan); mv.append(np.nan); continue
    vals = np.array([a[r.slot] for a in allp.values()]) * r.side
    mine = me[r.slot] * r.side
    rk.append(int((vals > mine).sum()) + 1)       # 1 = biggest gainer (CE) / loser (PE) at that moment
    nn.append(len(vals)); mv.append(mine * 100)
ev["rank"], ev["n"], ev["move_pct"] = rk, nn, mv
ev["share"] = ev["rank"] / ev["n"]
ds = sorted(ev.date.unique()); cut = ds[int(len(ds) * .6)]
ev["part"] = np.where(ev.date < cut, "train", "test")
def line(lbl, m):
    out = []
    for part in ("train", "test"):
        x = ev[m & (ev.part == part)]
        out.append(f"{part} {x.R.mean():+.3f}R ({len(x):>5}, {len(x)/ev[ev.part==part].date.nunique():.1f}/day, win {np.mean(x.R>0):.0%}, "
                   f"stopped {np.mean(x.how=='stop'):.0%})")
    print(f"  {lbl:<52} " + " | ".join(out))
print("Breaks by the stock's place among the day's movers at the break (R per trade, before costs)")
line("every break", ev.R.notna())
line("top 3 mover the break's way (~ the app's top 10)", ev["rank"] <= 3)
line("top 5 (top ~10%)", ev["rank"] <= 5)
line("top 10 (top ~20%)", ev["rank"] <= 10)
line("bottom half (moving the other way)", ev.share > 0.5)
line("top 3 + NIFTY same way + no wide day", (ev["rank"] <= 3) & (ev.nifty_ret > 0.05) & (ev.yday_ra <= 1.2))
line("top 5 + NIFTY same way + no wide day", (ev["rank"] <= 5) & (ev.nifty_ret > 0.05) & (ev.yday_ra <= 1.2))
line("top 3 and already up/down 2%+ on the day", (ev["rank"] <= 3) & (ev.move_pct >= 2))
print("\nTop 3, by year:", ev[ev["rank"] <= 3].groupby(pd.to_datetime(ev[ev['rank'] <= 3].date).dt.year).R.mean().round(3).to_dict())
