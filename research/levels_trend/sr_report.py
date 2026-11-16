import numpy as np, pandas as pd
T = pd.read_pickle("/home/claude/ml/lab/sr_trades.pkl")
L = pd.read_pickle("/home/claude/ml/lab/lab_trades.pkl")
ST = L[(L.strat == "Supertrend 15m flip") & (L.exit == "hold")]
alld = sorted(set(T.date)); CUT = alld[int(len(alld) * 0.6)]
ALLM = pd.period_range("2015-01", "2026-11", freq="M")
def st(x):
    d = pd.to_datetime(x.date); mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(ALLM, fill_value=0)
    yr = x.groupby(d.dt.year).pnl_cal.sum(); w = x.pnl_cal > 0
    return dict(per_mo=len(x)/len(ALLM), win10=10*w.mean(), avg_win=x.pnl_cal[w].mean(), avg_loss=-x.pnl_cal[~w].mean(),
                per_trade=x.pnl_cal.mean(), train=x[x.date < CUT].pnl_cal.mean(), test=x[x.date >= CUT].pnl_cal.mean(),
                R=x.R.mean(), rs_mo=mon.mean(), green_mo=np.mean(mon > 0), lose_yrs=int((yr < 0).sum()), stress=x.pnl_mkt.sum()/len(ALLM),
                risk=x.risk.median())
rows = [{"strategy": s, "exit": e, **st(x)} for (s, e), x in T.groupby(["strat", "exit"], observed=True)]
R = pd.DataFrame(rows)
pd.set_option("display.width", 250)
cols = ["strategy", "exit", "per_mo", "win10", "avg_win", "avg_loss", "per_trade", "train", "test", "R", "rs_mo", "green_mo", "lose_yrs", "stress", "risk"]
print("ALL (sorted by train):")
print(R.sort_values("train", ascending=False)[cols].round(2).to_string(index=False))
print("\nBEST EXIT PER STRATEGY (chosen on train):")
print(R.loc[R.groupby("strategy").train.idxmax()].sort_values("test", ascending=False)[cols].round(2).to_string(index=False))
# adding the best S/R trade next to the current system
orb = T[(T.strat == "ORB app rule") & (T.exit == "trail1")]
b = R[~R.strategy.str.startswith("ORB")].sort_values("train", ascending=False).iloc[0]
x = T[(T.strat == b.strategy) & (T.exit == b.exit)]
cur = pd.concat([orb, ST]); add = pd.concat([orb, ST, x])
for lbl, z in (("current system", cur), (f"current + {b.strategy} {b.exit}", add)):
    s = st(z); dd = z.groupby("date").pnl_cal.sum().sort_index().cumsum(); 
    print(f"{lbl:<55} ₹{s['rs_mo']:+,.0f}/mo, green months {s['green_mo']:.0%}, worst drawdown ₹{(dd-dd.cummax()).min():,.0f}, stress ₹{s['stress']:+,.0f}/mo")
