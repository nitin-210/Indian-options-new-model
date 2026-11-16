import pandas as pd, numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
df = pd.read_pickle("v2/brk_events.pkl")
ds = sorted(df.date.unique()); cut = ds[int(len(ds) * 0.6)]
tr, te = df[df.date < cut].copy(), df[df.date >= cut].copy()
feats = ["rvol_brk","rvol_day","rvol_or","rvol_last3","turnover_cr","or_atr","risk_atr","slot","ext","body","gap_atr",
         "dayret_atr","yday_dir","yday_ra","vs_pdh","poc_or","beyond_va","ahead_vol","beyond_va5","nifty_ret","rs","side"]
print("share reaching 2R: train %.1f%%  test %.1f%%" % (100 * (tr.mfe >= 2).mean(), 100 * (te.mfe >= 2).mean()))
m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.03, max_leaf_nodes=15, min_samples_leaf=300,
                                   l2_regularization=1.0, random_state=1).fit(tr[feats], tr.mfe >= 2)
te["p"] = m.predict_proba(te[feats])[:, 1]
print("AUC for a 2R+ move: train %.3f  test %.3f" % (roc_auc_score(tr.mfe >= 2, m.predict_proba(tr[feats])[:, 1]),
                                                   roc_auc_score(te.mfe >= 2, te.p)))
for f in ["rvol_day", "rvol_brk", "ahead_vol", "beyond_va5", "or_atr", "nifty_ret", "yday_ra", "turnover_cr"]:
    x = te[[f]].join(te.mfe >= 2).dropna()
    print(f"  test AUC of {f:<12} alone: {roc_auc_score(x.mfe, x[f]):.3f}")
for col in ["R", "R_tp1", "R_tp1.5", "R_tp2"]:
    row = []
    for n in (3,):
        top = te.sort_values("p", ascending=False).groupby("date").head(n)
        row.append(f"top-{n}/day by 2R-model {top[col].mean():+.3f}")
    filt = te[(te.nifty_ret > 0.05) & (te.yday_ra <= 1.2)]
    print(f"{col:<8} test all {te[col].mean():+.3f} | NIFTY-same-way & no wide day {filt[col].mean():+.3f} | " + " | ".join(row))
