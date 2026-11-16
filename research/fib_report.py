import pandas as pd, numpy as np
df = pd.read_pickle("fib_index.pkl")
df["d"] = pd.to_datetime(df["date"])
ds = np.sort(df.d.unique()); cut = ds[int(len(ds) * 0.6)]
df["part"] = np.where(df.d < cut, "train", "test")
order = ["current", "fib 38.2", "fib 50", "fib 61.8", "fib 50, stop 78.6", "fib 61.8, stop 100", "target 1.618 ext"]
months = {p: df[df.part == p].d.dt.to_period("M").nunique() for p in ("train", "test")}
print(f"signal days {df[df.variant=='current'].shape[0]}  ({df.d.min():%b %Y} - {df.d.max():%b %Y}); train to {pd.Timestamp(cut):%b %Y}")
for part in ("train", "test"):
    print(f"\n{part.upper()}  ({months[part]} months, 1 lot, after charges)")
    print(f"  {'variant':<20} {'trades':>6} {'filled':>7} {'win':>5} {'avg ₹':>8} {'total ₹':>11} {'₹/month':>8} {'worst month':>11}")
    for v in order:
        x = df[(df.variant == v) & (df.part == part)]
        f = x[x.filled]
        mon = x.groupby(x.d.dt.to_period("M")).pnl.sum()
        print(f"  {v:<20} {len(f):>6} {len(f)/len(x):>6.0%} {np.mean(f.pnl > 0):>5.0%} {f.pnl.mean():>+8,.0f} {x.pnl.sum():>+11,.0f} "
              f"{x.pnl.sum()/months[part]:>+8,.0f} {mon.min():>+11,.0f}")
print("\nBY YEAR, ₹ total (1 lot each, NIFTY + SENSEX)")
yr = df.pivot_table(index=df.d.dt.year, columns="variant", values="pnl", aggfunc="sum")[order]
print(yr.round(0).to_string())
# what the pullback entries miss: the current rule's result on days the 50% pullback never came
cur = df[df.variant == "current"].set_index(["key", "date"])
f50 = df[df.variant == "fib 50"].set_index(["key", "date"])
miss = cur[~f50.filled.reindex(cur.index).fillna(False).astype(bool)]
print(f"\nDays the 50% pullback never came: {len(miss)} of {len(cur)}; the current rule made ₹{miss.pnl.sum():+,.0f} on them "
      f"(avg ₹{miss.pnl.mean():+,.0f}), win {np.mean(miss.pnl > 0):.0%}")
