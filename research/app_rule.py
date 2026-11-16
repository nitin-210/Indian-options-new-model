"""Re-run the key comparison with the app's exact opening-range rule (skip breaks against yesterday's direction and
keep watching for one in the allowed direction), plus Supertrend 15m, trailing vs hold."""
import sys
import numpy as np
import pandas as pd
sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402


def app_orb(a, I, F):
    if F.get("yra") is None or F["yra"] > 1.2 or F.get("ydir") is None:
        return None
    o, h, l, c = a.T
    H, L = h[:3].max(), l[:3].min()
    w = H - L
    if w <= 0:
        return None
    sd = F["ydir"]
    for i in range(3, lab.LAST_SIG + 1):
        if (c[i] > H + 0.2 * w) if sd == 1 else (c[i] < L - 0.2 * w):
            return i, sd, (L if sd == 1 else H)
    return None


vix = lab.load_vix()
rows = []
for key in ("NIFTY", "SENSEX"):
    days, ind, fac = lab.prepare(key)
    for d in sorted(days):
        if d < lab.START[key] or "atr" not in fac[d]:
            continue
        s = app_orb(days[d], ind[d], fac[d])
        if not s:
            continue
        i, sd, stop = s
        ex, risk = lab.exits(days[d], i, sd, stop)
        if ex is None:
            continue
        vx = vix.get(d)
        iv = float(vx[i]) / 100 if vx is not None and np.isfinite(vx[i]) else 0.14
        for exn in ("hold", "trail1"):
            px, j, half = ex[exn]
            rows.append((key, d, "APP RULE", exn, sd, float(days[d][i, 3]), (i + 1) * 5, iv, odds.weekly_expiry_for(key, d),
                         (px - days[d][i, 3]) * sd / risk, risk, i, float(px), (j + 1) * 5, 1.0))
A = pd.DataFrame(rows, columns=lab.COLS)
for m in ("cal", "mkt"):
    A[f"pnl_{m}"] = lab.opt_pnl(A, m)
A.to_pickle("/home/claude/ml/lab/app_rule.pkl")
df = pd.read_pickle("/home/claude/ml/lab/lab_trades.pkl")
ST = df[(df.strat == "Supertrend 15m flip") & (df.exit == "hold")]
sets = {"App rule, hold (as it was)": A[A.exit == "hold"], "App rule, trailing exit (new)": A[A.exit == "trail1"],
        "Supertrend 15m alone": ST,
        "App rule hold + Supertrend": pd.concat([A[A.exit == "hold"], ST]),
        "App rule trailing + Supertrend (new)": pd.concat([A[A.exit == "trail1"], ST])}
allm = pd.period_range("2015-01", "2026-11", freq="M")
for k, x in sets.items():
    d = pd.to_datetime(x.date)
    mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(allm, fill_value=0)
    yr = x.groupby(d.dt.year).pnl_cal.sum()
    day = x.groupby("date").pnl_cal.sum()
    w = x.pnl_cal > 0
    p = x.pnl_cal.to_numpy()
    blocks = [p[i:i + 10].sum() for i in range(0, len(p) - 9, 10)]
    print(f"{k:<38} {len(x)/len(allm):>4.1f}/mo | of 10: {10*w.mean():.1f} win, avg win ₹{x.pnl_cal[w].mean():,.0f} loss ₹{-x.pnl_cal[~w].mean():,.0f} "
          f"| per 10 ₹{10*x.pnl_cal.mean():+,.0f} | green days {np.mean(day>0):.0%} months {np.mean(mon>0):.0%} | ₹{mon.mean():+,.0f}/mo "
          f"(last 3y ₹{mon[-35:].mean():+,.0f}) | worst mo ₹{mon.min():+,.0f} | losing yrs {int((yr<0).sum())} | stress ₹{x.pnl_mkt.sum()/len(allm):+,.0f}/mo")
print("\nby year, app rule trailing + Supertrend:",
      {y: round(v) for y, v in sets["App rule trailing + Supertrend (new)"].groupby(pd.to_datetime(sets["App rule trailing + Supertrend (new)"].date).dt.year).pnl_cal.sum().items()})
