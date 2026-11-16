import numpy as np, pandas as pd
T = pd.read_pickle("orb_pdhl.pkl")
A = pd.read_pickle("app_rule.pkl"); APP = A[A.exit == "trail1"]
def months(keys):
    return pd.period_range("2022-10" if keys == ("BANKNIFTY",) else "2015-01", "2026-11", freq="M")
def st(x, lbl, keys=("NIFTY", "SENSEX")):
    M = months(keys); alld = sorted(set(x.date)) or [None]
    cut = pd.Timestamp("2022-02-23").date() if keys != ("BANKNIFTY",) else pd.Timestamp("2025-02-01").date()
    d = pd.to_datetime(x.date); mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(M, fill_value=0)
    yr = x.groupby(d.dt.year).pnl_cal.sum(); w = x.pnl_cal > 0
    print(f"  {lbl:<52} {len(x)/len(M):>4.1f}/mo | win {10*w.mean():.1f}/10 | avg win ₹{x.pnl_cal[w].mean():,.0f} loss ₹{-x.pnl_cal[~w].mean():,.0f} | "
          f"₹/trade {x.pnl_cal.mean():+,.0f} (train {x[x.date<cut].pnl_cal.mean():+,.0f} test {x[x.date>=cut].pnl_cal.mean():+,.0f}) | "
          f"₹{mon.mean():+,.0f}/mo | green mo {np.mean(mon>0):.0%} | losing yrs {int((yr<0).sum())} | stress ₹{x.pnl_mkt.sum()/len(M):+,.0f}/mo")
for keys in (("NIFTY", "SENSEX"), ("BANKNIFTY",)):
    X = T[T.key.isin(keys)]
    print("\n" + "=" * 20, " + ".join(keys))
    print("  how days open:", (X[(X.exit == "hold") & (X.strat == "trigger at the 15-min high/low")].open_at.value_counts(normalize=True).round(2).to_dict()))
    for tag in ("trigger at the 15-min high/low", "trigger 0.2x beyond (app)"):
        for ex in ("hold", "trail1"):
            print(f" [{tag}, exit {ex}]")
            Y = X[(X.strat == tag) & (X.exit == ex)]
            st(Y, "every 15-minute breakout", keys)
            for g in ("DOUBLE CONFIRMATION (gap + same-way break)", "gap, but broke the other way", "opened inside, broke up", "opened inside, broke down"):
                st(Y[Y.group == g], g, keys)
            st(Y[(Y.group.str.startswith("DOUBLE")) & Y.yday_same & ~Y.after_wide], "DOUBLE CONF. + yesterday same way + no wide day", keys)
    Z = X[(X.strat == "beyond BOTH 15-min and yesterday's high/low") & (X.exit == "trail1") & (X.open_at == "inside")]
    print(" [opened inside: wait until beyond BOTH the 15-min and yesterday's high/low, trailing exit]")
    st(Z, "inside open, beyond both", keys)
    if keys != ("BANKNIFTY",):
        print(" [the app's current rule (yesterday's direction, skip after wide day, trailing exit), by where the day opened]")
        st(APP, "app rule, all", keys)
        M2 = APP.merge(X[(X.strat == "trigger 0.2x beyond (app)") & (X.exit == "hold")][["key", "date", "open_at"]], on=["key", "date"], how="left")
        for w_ in ("above", "below", "inside"):
            sub = M2[M2.open_at == w_]
            sub = sub[((sub.open_at == "above") & (sub.sd == 1)) | ((sub.open_at == "below") & (sub.sd == -1)) | (sub.open_at == "inside")] if w_ != "inside" else sub
            st(sub, f"app rule, opened {w_}" + (" (same-way trades)" if w_ != "inside" else ""), keys)
