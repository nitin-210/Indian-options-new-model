"""Report for m1lab.py (indices). Ranked on the TRAIN days only (first 60% of dates); the last 40% is the test."""
import numpy as np
import pandas as pd

T = pd.read_pickle("/home/claude/ml/m1/m1_trades.pkl")
A = pd.read_pickle("/home/claude/ml/lab/app_rule.pkl")
alld = sorted(set(T.date))
CUT = alld[int(len(alld) * 0.6)]
ALLM = pd.period_range(min(alld), max(alld), freq="M")
print(f"1-minute data: {min(alld)} -> {max(alld)}; train before {CUT}, test from {CUT}. "
      f"NIFTY {T[T.key=='NIFTY'].date.nunique()} days traded-on, SENSEX {T[T.key=='SENSEX'].date.nunique()}")


def stats(x):
    d = pd.to_datetime(x.date)
    mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(ALLM, fill_value=0)
    yr = x.groupby(d.dt.year).pnl_cal.sum()
    w = x.pnl_cal > 0
    tr, te = x[x.date < CUT], x[x.date >= CUT]
    return dict(per_mo=len(x) / len(ALLM), win10=10 * w.mean(), avg_win=x.pnl_cal[w].mean(),
                avg_loss=-x.pnl_cal[~w].mean(), per_trade=x.pnl_cal.mean(), train=tr.pnl_cal.mean(),
                test=te.pnl_cal.mean(), R=x.R.mean(), rs_mo=mon.mean(), green_mo=np.mean(mon > 0),
                worst_mo=mon.min(), lose_yrs=int((yr < 0).sum()), stress=x.pnl_mkt.sum() / len(ALLM),
                risk_pts=x.risk.median())


rows = []
for (s, e), x in T.groupby(["strat", "exit"], observed=True):
    rows.append({"strategy": s, "exit": e, **stats(x)})
R = pd.DataFrame(rows)
pd.set_option("display.width", 260); pd.set_option("display.max_colwidth", 48)
cols = ["strategy", "exit", "per_mo", "win10", "avg_win", "avg_loss", "per_trade", "train", "test", "rs_mo", "green_mo",
        "lose_yrs", "stress", "risk_pts"]
for grp, pref in (("OUR RULES, 1-MINUTE EXECUTION", ("ORB", "Supertrend")), ("MULTI-TIMEFRAME 1-MINUTE SETUPS", ("MTF", "ORB5"))):
    print("\n" + "=" * 30, grp, "(₹ per trade, 1 lot, app pricing; stress = all decay in market hours)")
    x = R[R.strategy.str.startswith(pref)].sort_values(["strategy", "train"], ascending=[True, False])
    print(x[cols].round(1).to_string(index=False))

print("\n" + "=" * 30, "BEST EXIT PER STRATEGY (chosen on train), sorted by test ₹/trade")
best = R.loc[R.groupby("strategy").train.idxmax()].sort_values("test", ascending=False)
print(best[cols].round(1).to_string(index=False))

# consistency: baseline on 1-minute candles vs the 5-minute research simulation on the same days
b = T[(T.strat == "ORB app: 5m close entry (baseline)") & (T.exit == "trail5")]
a = A[(A.exit == "trail1")]
j = b.merge(a, on=["key", "date"], suffixes=("_1m", "_5m"))
print(f"\ncheck: baseline on 1m vs 5m research, same {len(j)} trades: same side {np.mean(j.sd_1m == j.sd_5m):.0%}, "
      f"same entry price {np.mean(abs(j.S0_1m - j.S0_5m) < 0.5):.0%}, ₹/trade {j.pnl_cal_1m.mean():+,.0f} (1m) vs "
      f"{j.pnl_cal_5m.mean():+,.0f} (5m)")
R.to_pickle("/home/claude/ml/m1/m1_summary.pkl")
