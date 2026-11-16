import pandas as pd, numpy as np
df = pd.read_pickle("v2/brk_events.pkl")
ds = sorted(df.date.unique()); cut = ds[int(len(ds) * 0.6)]
df["part"] = np.where(df.date < cut, "train", "test")
feats = ["rvol_brk","rvol_day","rvol_or","rvol_last3","turnover_cr","or_atr","risk_atr","slot","ext","body","gap_atr",
         "dayret_atr","yday_dir","yday_ra","vs_pdh","poc_or","beyond_va","ahead_vol","beyond_va5","nifty_ret","rs"]
pd.set_option("display.width", 200)
for f in feats:
    x = df[[f, "R", "part"]].dropna().copy()
    if x[f].nunique() <= 3:
        g = x.groupby([f, "part"]).R.mean().unstack()
    else:
        qs = np.array(x[x.part=="train"][f].quantile([0, .2, .4, .6, .8, 1]).values, dtype=float)
        qs[0], qs[-1] = -np.inf, np.inf
        x["b"] = pd.cut(x[f], np.unique(qs))
        g = x.groupby(["b", "part"], observed=True).R.mean().unstack()
    print(f"== {f}: " + " | ".join(f"{str(i)[:18]:>18} tr {r['train']:+.3f} te {r['test']:+.3f}" for i, r in g.iterrows()))
