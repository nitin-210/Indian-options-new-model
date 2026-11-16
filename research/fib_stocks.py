"""Fibonacci pullback entries on the stock range breaks, and pre-market top-3 combined with the shortlist filters.
P&L per trade in ATR units for 1 lot (so a smaller-risk entry is not flattered), option costs about 0.06 ATR a trade."""
import sys
import numpy as np, pandas as pd
sys.path.insert(0, "/home/claude/ml/v2")
from brk_events import D, load, T930, T1355, T1455  # noqa: E402
rows = []
for p in sorted((D / "stocks5m").glob("*_5m.json")):
    name = p.stem.replace("_5m", "")
    days = load(p); ds = sorted(days)
    for i in range(21, len(ds)):
        d = ds[i]; a = days[d]; o, h, l, c, v = a.T
        atr = np.mean([days[x][:, 1].max() - days[x][:, 2].min() for x in ds[i - 14:i]])
        H, L = h[:3].max(), l[:3].min(); w = H - L
        if w <= 0 or atr <= 0:
            continue
        up, dn = H + 0.2 * w, L - 0.2 * w
        k = sd = None
        for j in range(T930, T1355 + 1):
            if c[j] > up: k, sd = j, 1; break
            if c[j] < dn: k, sd = j, -1; break
        if k is None:
            continue
        stop = L if sd == 1 else H
        def walk(entry, j0):
            for j in range(j0, T1455 + 1):
                if (l[j] <= stop) if sd == 1 else (h[j] >= stop):
                    return (min(stop, o[j]) if sd == 1 else max(stop, o[j]))
            return c[T1455]
        res = {"name": name, "date": d, "current": (walk(c[k], k + 1) - c[k]) * sd / atr}
        A = l[:k + 1].min() if sd == 1 else h[:k + 1].max()
        for lbl, X in (("fib 38.2", .382), ("fib 50", .5), ("fib 61.8", .618)):
            B = h[k] if sd == 1 else l[k]; val = np.nan
            for j in range(k + 1, T1455 + 1):
                if j > T1355:
                    break
                lvl = B - X * (B - A) if sd == 1 else B + X * (A - B)
                if (l[j] <= lvl) if sd == 1 else (h[j] >= lvl):
                    e = min(lvl, o[j]) if sd == 1 else max(lvl, o[j])
                    if (l[j] <= stop) if sd == 1 else (h[j] >= stop):
                        val = (stop - e) * sd / atr
                    else:
                        val = (walk(e, j + 1) - e) * sd / atr
                    break
                B = max(B, h[j]) if sd == 1 else min(B, l[j])
            res[lbl] = val
        rows.append(res)
f = pd.DataFrame(rows)
ds_ = sorted(f.date.unique()); cut = ds_[int(len(ds_) * .6)]
f["part"] = np.where(f.date < cut, "train", "test")
COST = 0.06
print("FIBONACCI ON STOCK BREAKS (55 large caps, per trade in ATR units, 1 lot; costs ~0.06 ATR a trade)")
for part in ("train", "test"):
    x = f[f.part == part]; days = x.date.nunique()
    for col in ("current", "fib 38.2", "fib 50", "fib 61.8"):
        y = x[col].dropna()
        print(f"  {part:<5} {col:<9} trades {len(y):>6} ({len(y)/len(x):.0%} filled)  avg {y.mean():+.4f} ATR  "
              f"after costs {y.mean()-COST:+.4f}  win {np.mean(y>0):.0%}")
# pre-market top 3 + shortlist filters
pm = pd.read_pickle("premarket_stocks.pkl"); ev = pd.read_pickle("brk_events.pkl")[["name", "date", "nifty_ret", "yday_ra"]]
pm = pm.merge(ev, on=["name", "date"], how="left")
pm["part"] = np.where(pm.date < cut, "train", "test")
print("\nPRE-MARKET TOP 3 (highest opening volume) + NIFTY the same way + no wide day before (R per trade, before costs)")
for part in ("train", "test"):
    x = pm[pm.part == part]
    t = x.sort_values("vol0", ascending=False).groupby("date").head(3)
    t = t[t.side.notna()]
    q = t[(t.nifty_ret > 0.05) & (t.yday_ra <= 1.2)]
    print(f"  {part:<5} top 3 only: {t.R.mean():+.3f}R ({len(t)} trades) | with both filters: {q.R.mean():+.3f}R ({len(q)} trades, "
          f"{len(q)/x.date.nunique():.1f}/day, win {np.mean(q.R>0):.0%})")
