import sys, numpy as np, pandas as pd
sys.path.insert(0, "/home/claude/ml/lab")
import lab, liquidity as Q
from app import odds
vix = lab.load_vix(); rows = []
for key in ("NIFTY", "SENSEX"):
    days, ind, fac = lab.prepare(key); ds = sorted(days); D15 = Q.dir15_all(days, ds)
    for j, d in enumerate(ds):
        if d < lab.START[key] or "atr" not in fac[d] or j < 6: continue
        prev = [days[x] for x in ds[j - 5:j]]; F = fac[d]; a = days[d]
        s = Q.find(a, Q.day_pools(prev, F, F["atr"]), Q.trendlines(prev, F["atr"]), F["atr"], D15[j], "mss")
        if not s: continue
        i, sd, stop, wt = s
        if wt or (a[i, 3] - stop) * sd <= 0: continue
        ex, risk = lab.exits(a, i, sd, stop)
        if ex is None: continue
        vx = vix.get(d); iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
        for exn, (px, jj, _) in ex.items():
            if exn == "half1": continue
            rows.append((key, d, "sweep reversal", exn, sd, float(a[i, 3]), (i + 1) * 5, iv, odds.weekly_expiry_for(key, d), (px - a[i, 3]) * sd / risk, risk, i, float(px), (jj + 1) * 5, 1.0))
T = pd.DataFrame(rows, columns=lab.COLS)
for m in ("cal", "mkt"): T[f"pnl_{m}"] = lab.opt_pnl(T, m)
T.to_pickle("/home/claude/ml/lab/mss_final.pkl")
A = pd.read_pickle("app_rule.pkl"); ORB = A[A.exit == "trail1"]
L = pd.read_pickle("lab_trades.pkl"); ST = L[(L.strat == "Supertrend 15m flip") & (L.exit == "hold")]
allm = pd.period_range("2015-01", "2026-11", freq="M"); cut = pd.Timestamp("2022-02-23").date()
for e, x in T.groupby("exit"):
    w = x.pnl_cal > 0
    print(f"  {e:<6} win {10*w.mean():.1f}/10 avg win ₹{x.pnl_cal[w].mean():,.0f} loss ₹{-x.pnl_cal[~w].mean():,.0f} ₹/trade {x.pnl_cal.mean():+,.0f} (train {x[x.date<cut].pnl_cal.mean():+,.0f} test {x[x.date>=cut].pnl_cal.mean():+,.0f}) stress {x.pnl_mkt.mean():+,.0f}")
X = T[T.exit == "hold"]
print("by year:", {y: int(v) for y, v in X.groupby(pd.to_datetime(X.date).dt.year).pnl_cal.sum().items()})
print("by index ₹/trade:", X.groupby("key").pnl_cal.mean().round(0).to_dict())
def st(z, lbl, k=1.0):
    d = pd.to_datetime(z.date); mon = z.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(allm, fill_value=0) * k
    dd = z.groupby("date").pnl_cal.sum().sort_index().cumsum() * k; yr = z.groupby(d.dt.year).pnl_cal.sum()
    w = z.pnl_cal > 0
    print(f"{lbl:<46} {len(z)/len(allm):>5.1f}/mo | win {10*w.mean():.1f}/10 | ₹{mon.mean():+,.0f}/mo (last 3y ₹{mon[-35:].mean():+,.0f}) | green mo {np.mean(mon>0):.0%} | worst mo ₹{mon.min():+,.0f} | worst drawdown ₹{(dd-dd.cummax()).min():,.0f} | losing yrs {int((yr<0).sum())} | stress ₹{z.pnl_mkt.sum()/len(allm)*k:+,.0f}/mo")
cur = pd.concat([ORB, ST]); add = pd.concat([ORB, ST, X])
st(cur, "current system")
st(X, "sweep reversal alone")
st(add, "current + sweep reversal")
k = add.pnl_cal.sum() / cur.pnl_cal.sum()
st(cur, f"current system x{k:.2f} lots (same money)", k)
c2 = cur.groupby(["key", "date"]).agg(sd=("sd", "first"), p=("pnl_cal", "sum")); x = X.set_index(["key", "date"]); both = x.index.intersection(c2.index)
print(f"same-day overlap {len(both)/len(X):.0%}, opposite direction {np.mean(x.loc[both].sd.to_numpy() != c2.loc[both].sd.to_numpy()):.0%}, correlation {np.corrcoef(x.loc[both].pnl_cal, c2.loc[both].p)[0,1]:.2f}")
