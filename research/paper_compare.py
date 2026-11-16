import numpy as np, pandas as pd
a = pd.read_pickle("paper_strategies.pkl"); b = pd.read_pickle("paper_strategies_trading.pkl")
def summ(df):
    df = df.copy(); df["d"] = pd.to_datetime(df["date"])
    ds = np.sort(df.d.unique()); cut = ds[int(len(ds) * 0.6)]
    df["part"] = np.where(df.d < cut, "train", "test")
    M = {p: df[df.part == p].d.dt.to_period("M").nunique() for p in ("train", "test")}
    out = {}
    for (m, v, s), g in df.groupby(["mode", "view", "structure"]):
        if s == "no trade": continue
        te, tr = g[g.part == "test"], g[g.part == "train"]
        out[(m, v, s)] = (tr.pnl.sum() / M["train"], te.pnl.sum() / M["test"], np.mean(g.pnl > 0),
                          g.groupby(g.d.dt.to_period("M")).pnl.sum().min())
    return out
A, B = summ(a), summ(b)
print(f"{'':<52} {'calendar-time decay (now)':>28} | {'all decay in market hours':>28}")
print(f"{'':<52} {'train/mo':>9} {'test/mo':>9} {'win':>4} | {'train/mo':>9} {'test/mo':>9} {'win':>4}  worst month (trading)")
for k in sorted(A, key=lambda k: (k[0], k[1], k[2])):
    x, y = A[k], B[k]
    print(f"{k[0] + ' | ' + (k[1] + ': ' if k[1] != 'none' else '') + k[2]:<52} {x[0]:>+9,.0f} {x[1]:>+9,.0f} {x[2]:>4.0%} | {y[0]:>+9,.0f} {y[1]:>+9,.0f} {y[2]:>4.0%}  {y[3]:>+10,.0f}")
