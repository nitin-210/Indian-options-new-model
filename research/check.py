"""Check the candidate changes year by year, with realistic timing:
  A  our model as it is (hold)                         B  our model, trail the stop 1R behind after +1R
  C  only days NIFTY and SENSEX both signal the same side, both entered when the SECOND one signals (hold)
  D  = C with the trailing exit                        E  our model + Supertrend 15m flips (hold), each 1 lot
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402

df = pd.read_pickle("/home/claude/ml/lab/lab_trades.pkl")
OURS = "OUR MODEL (ORB15, yday, no wide day)"
vix = lab.load_vix()
P = {k: lab.prepare(k) for k in ("NIFTY", "SENSEX")}
base = df[(df.strat == OURS) & (df.exit == "hold")]
both = base.groupby("date").filter(lambda g: len(g) == 2 and g.sd.nunique() == 1)
rows = []
from app import odds  # noqa: E402
for d, g in both.groupby("date"):
    i2 = int(g.t_in.max())
    for r in g.itertuples():
        days, ind, fac = P[r.key]
        a = days[d]
        H, L = a[:3, 1].max(), a[:3, 2].min()
        stop = L if r.sd == 1 else H
        ex, risk = lab.exits(a, i2, int(r.sd), stop)
        if ex is None:
            continue
        vx = vix.get(d)
        iv = float(vx[i2]) / 100 if vx is not None and np.isfinite(vx[i2]) else 0.14
        for exn in ("hold", "trail1"):
            px, j, half = ex[exn]
            rows.append((r.key, d, f"C/D {exn}", exn, int(r.sd), float(a[i2, 3]), (i2 + 1) * 5, iv, odds.weekly_expiry_for(r.key, d),
                         (px - a[i2, 3]) * r.sd / risk, risk, i2, float(px), (j + 1) * 5, 1.0))
C = pd.DataFrame(rows, columns=lab.COLS)
for m in ("cal", "mkt"):
    C[f"pnl_{m}"] = lab.opt_pnl(C, m)
sets = {"A our model (now)": base,
        "B + trailing exit": df[(df.strat == OURS) & (df.exit == "trail1")],
        "C both indices agree (enter at 2nd signal)": C[C.exit == "hold"],
        "D = C + trailing exit": C[C.exit == "trail1"],
        "E our model + Supertrend 15m": pd.concat([base, df[(df.strat == "Supertrend 15m flip") & (df.exit == "hold")]])}
print("₹ per year, NIFTY + SENSEX, 1 lot per trade, current pricing (stress-test pricing in the last columns)")
yrs = list(range(2015, 2027))
print(f"{'':<44}" + "".join(f"{y:>8}" for y in yrs) + f"{'total':>10}{'win':>5}{'green':>6}{'losing yrs':>11}{'stress total':>13}")
for k, x in sets.items():
    x = x.copy(); x["y"] = pd.to_datetime(x["date"]).dt.year
    yr = x.groupby("y").pnl_cal.sum()
    day = x.groupby("date").pnl_cal.sum()
    print(f"{k:<44}" + "".join(f"{yr.get(y, 0):>+8,.0f}" for y in yrs) + f"{x.pnl_cal.sum():>+10,.0f}{np.mean(x.pnl_cal > 0):>5.0%}"
          f"{np.mean(day > 0):>6.0%}{int((yr < 0).sum()):>11}{x.pnl_mkt.sum():>+13,.0f}")
    print(f"{'   trades':<44}" + "".join(f"{(x.y == y).sum():>8}" for y in yrs))
