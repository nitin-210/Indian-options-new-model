import sys, itertools, numpy as np, pandas as pd
sys.path.insert(0, "/home/claude/ml/lab")
import lab, liquidity as Q
from app import odds
vix = lab.load_vix()
data = {}
for key in ("NIFTY", "SENSEX"):
    days, ind, fac = lab.prepare(key); ds = sorted(days); D15 = Q.dir15_all(days, ds)
    pre = []
    for j, d in enumerate(ds):
        if d < lab.START[key] or "atr" not in fac[d] or j < 6: continue
        prev = [days[x] for x in ds[j - 5:j]]
        pre.append((d, days[d], fac[d], Q.day_pools(prev, fac[d], fac[d]["atr"]), Q.trendlines(prev, fac[d]["atr"]), D15[j]))
    data[key] = pre
cut = pd.Timestamp("2022-02-23").date()
def run(kind, **kw):
    rows = []
    for key, pre in data.items():
        for d, a, F, pools, lines, d15 in pre:
            s = Q.find(a, pools, lines, F["atr"], d15, kind, **kw)
            if not s: continue
            i, sd, stop, wt = s
            if kind == "mss" and wt: continue          # the against-trend version
            if (a[i, 3] - stop) * sd <= 0: continue
            ex, risk = lab.exits(a, i, sd, stop)
            if ex is None: continue
            vx = vix.get(d); iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
            px, jj, _ = ex["hold"]
            rows.append((key, d, kind, "hold", sd, float(a[i, 3]), (i + 1) * 5, iv, odds.weekly_expiry_for(key, d), (px - a[i, 3]) * sd / risk, risk, i, float(px), (jj + 1) * 5, 1.0))
    T = pd.DataFrame(rows, columns=lab.COLS); T["pnl"] = lab.opt_pnl(T, "cal"); T["pm"] = lab.opt_pnl(T, "mkt")
    return T
out = []
for DEPTH, JEND in []:
    T = run("judas", DEPTH=DEPTH, JEND=JEND)
    out.append(dict(setup="judas", DEPTH=DEPTH, param=f"until {(555 + (JEND + 1) * 5) // 60}:{(555 + (JEND + 1) * 5) % 60:02d}", n=len(T),
                    rs=T.pnl.mean(), train=T[T.date < cut].pnl.mean(), test=T[T.date >= cut].pnl.mean(), stress=T.pm.mean()))
for DEPTH, WIN in itertools.product((0.01, 0.02, 0.03, 0.05, 0.08), (6, 12, 18)):
    T = run("mss", DEPTH=DEPTH, WIN=WIN)
    out.append(dict(setup="mss against trend", DEPTH=DEPTH, param=f"shift within {WIN * 5} min", n=len(T),
                    rs=T.pnl.mean(), train=T[T.date < cut].pnl.mean(), test=T[T.date >= cut].pnl.mean(), stress=T.pm.mean()))
R = pd.DataFrame(out); pd.set_option("display.width", 200)
print(R.round(1).to_string(index=False))
for s, x in R.groupby("setup"):
    print(f"{s}: positive {int((x.rs > 0).sum())}/{len(x)}, positive train AND test {int(((x.train > 0) & (x.test > 0)).sum())}/{len(x)}, "
          f"median ₹/trade {x.rs.median():+.0f} (train {x.train.median():+.0f}, test {x.test.median():+.0f}), median stress ₹/trade {x.stress.median():+.0f}")
