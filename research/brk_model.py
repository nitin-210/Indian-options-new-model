import pandas as pd, numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
df = pd.read_pickle("v2/brk_events.pkl")
ds = sorted(df.date.unique()); cut = ds[int(len(ds) * 0.6)]
tr, te = df[df.date < cut].copy(), df[df.date >= cut].copy()
feats = ["rvol_brk","rvol_day","rvol_or","rvol_last3","turnover_cr","or_atr","risk_atr","slot","ext","body","gap_atr",
         "dayret_atr","yday_dir","yday_ra","vs_pdh","poc_or","beyond_va","ahead_vol","beyond_va5","nifty_ret","rs","side"]
y = tr.R.clip(-1, 3)
m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.03, max_leaf_nodes=15, min_samples_leaf=300,
                                  l2_regularization=1.0, random_state=1).fit(tr[feats], y)
te["p"] = m.predict(te[feats]); tr["p"] = m.predict(tr[feats])
print("corr pred vs R  train %.3f  test %.3f" % (np.corrcoef(tr.p, tr.R)[0,1], np.corrcoef(te.p, te.R)[0,1]))
for name, x in (("train", tr), ("test", te)):
    x["q"] = pd.qcut(x.p, 5, labels=False)
    print(name, "R by predicted quintile:", x.groupby("q").R.mean().round(3).tolist())
for n in (2, 3, 4):
    top = te.sort_values("p", ascending=False).groupby("date").head(n)
    print(f"test top-{n}/day: trades {len(top)}, mean R {top.R.mean():+.3f}, win {np.mean(top.R>0):.0%}, "
          f"stopped {np.mean(top.how=='stop'):.0%}, total R {top.R.sum():+.0f} over {top.date.nunique()} days")
# simple filters
def show(lbl, mask, x):
    s = x[mask]
    print(f"{lbl:<48} n={len(s):>6} meanR {s.R.mean():+.3f} win {np.mean(s.R>0):.0%}")
for name, x in (("train", tr), ("test", te)):
    print("--", name)
    show("all", x.R.notna(), x)
    show("NIFTY moving the same way (>0.05%)", x.nifty_ret > 0.05, x)
    show("not after a wide day (yday range <=1.2 ATR)", x.yday_ra <= 1.2, x)
    show("both", (x.nifty_ret > 0.05) & (x.yday_ra <= 1.2), x)
    show("both + rvol_day>1.2", (x.nifty_ret > 0.05) & (x.yday_ra <= 1.2) & (x.rvol_day > 1.2), x)
    show("both + turnover top 40%", (x.nifty_ret > 0.05) & (x.yday_ra <= 1.2) & (x.turnover_cr > 450), x)
