import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score
pm = pd.read_pickle("premarket_stocks.pkl")
ds = sorted(pm.date.unique()); cut = ds[int(len(ds) * 0.6)]
pm["part"] = np.where(pm.date < cut, "train", "test")
pm["absgap"] = pm.gap_atr.abs(); pm["absrel"] = pm.rel_gap.abs(); pm["outside"] = (pm.open_pos != 0).astype(int)
pm["gside"] = np.sign(pm.gap_atr)
pm["inplay"] = pm.groupby("date").day_range_atr.rank(pct=True) >= 0.8        # top 20% range of the day
print(f"stock-days {len(pm)}, train to {cut}; a break happened on {pm.side.notna().mean():.0%} of stock-days")

print("\n1) Does the pre-market tell which stocks will move a lot today? (AUC for being in the day's top-20% range;"
      " 0.5 = no idea)")
F = ["absgap", "vol0", "absrel", "outside", "y_ra", "turn"]
for f in F:
    print(f"   {f:<8} train {roc_auc_score(pm[pm.part=='train'].inplay, pm[pm.part=='train'][f]):.3f}"
          f"  test {roc_auc_score(pm[pm.part=='test'].inplay, pm[pm.part=='test'][f]):.3f}")
feats = ["gap_atr", "absgap", "rel_gap", "absrel", "open_pos", "vol0", "y_clv", "y_ret_atr", "y_ra", "mom5_atr", "turn", "nifty_gap"]
tr = pm[pm.part == "train"]
m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                  random_state=1).fit(tr[feats], tr.day_range_atr)
pm["p_range"] = m.predict(pm[feats])
for part in ("train", "test"):
    x = pm[pm.part == part]
    print(f"   model (all pre-market facts) {part}: AUC {roc_auc_score(x.inplay, x.p_range):.3f}")

print("\n2) Trade only the top-N stocks by a pre-market score (their range break; R per trade before costs;"
      " option costs about 0.10R)")
scores = {"biggest gap": "absgap", "highest opening volume": "vol0", "biggest gap vs NIFTY": "absrel",
          "model: biggest expected range": "p_range"}
pm["combo"] = pm.groupby("date").absgap.rank(pct=True) + pm.groupby("date").vol0.rank(pct=True) \
    + pm.groupby("date").absrel.rank(pct=True)
scores["gap + volume + gap vs NIFTY"] = "combo"
def pick(x, col, n, mode):
    top = x.sort_values(col, ascending=False).groupby("date").head(n)
    t = top[top.side.notna()].copy()
    sd = np.where(t.side == 1, 1, -1)
    if mode == "with gap":
        t = t[sd == t.gside]
    elif mode == "against gap":
        t = t[sd == -t.gside]
    return t
for lbl, col in scores.items():
    for mode in ("either side", "with gap", "against gap"):
        out = []
        for part in ("train", "test"):
            t = pick(pm[pm.part == part], col, 3, mode)
            out.append(f"{part} {t.R.mean():+.3f}R ({len(t):>4} trades, {len(t)/pm[pm.part==part].date.nunique():.1f}/day, win {np.mean(t.R>0):.0%})")
        print(f"   top 3 by {lbl:<30} {mode:<12} " + " | ".join(out))
print("\n   for comparison: every break  train %+.3fR | test %+.3fR" % (pm[(pm.part=='train')].R.mean(), pm[(pm.part=='test')].R.mean()))
for n in (2, 4):
    out = []
    for part in ("train", "test"):
        t = pick(pm[pm.part == part], "p_range", n, "either side")
        out.append(f"{part} {t.R.mean():+.3f}R ({len(t)} trades)")
    print(f"   top {n} by the range model, either side: " + " | ".join(out))
print("\n3) By year, top 3 by the range model, either side (R per trade):",
      pick(pm, "p_range", 3, "either side").groupby(pd.to_datetime(pick(pm, "p_range", 3, "either side").date).dt.year).R.mean().round(3).to_dict())
