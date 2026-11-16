import sys, itertools, numpy as np, pandas as pd
sys.path.insert(0, "/home/claude/ml/lab")
import lab, zones
vix = lab.load_vix()
out = []
for MOVE, WIDTH, LIFE, AWAY in itertools.product((0.4, 0.6, 0.8), (0.1, 0.2), (3, 5), (0.3, 0.5)):
    rows = zones.run_index("NIFTY", vix, MOVE, WIDTH, LIFE, AWAY) + zones.run_index("SENSEX", vix, MOVE, WIDTH, LIFE, AWAY)
    T = pd.DataFrame(rows, columns=lab.COLS)
    T = T[(T.strat == "zone retest: zone already held once") & (T.exit.isin(["hold", "trail1"]))]
    T["pnl"] = lab.opt_pnl(T, "cal")
    alld = pd.Series(sorted(set(T.date)))
    for e, x in T.groupby("exit"):
        cut = pd.Timestamp("2022-08-19").date()
        out.append(dict(MOVE=MOVE, WIDTH=WIDTH, LIFE=LIFE, AWAY=AWAY, exit=e, n=len(x), win10=10*np.mean(x.pnl > 0),
                        rs=x.pnl.mean(), train=x[x.date < cut].pnl.mean(), test=x[x.date >= cut].pnl.mean(), n_test=int((x.date >= cut).sum())))
R = pd.DataFrame(out)
pd.set_option("display.width", 200)
print(R.round(0).to_string(index=False))
for e in ("hold", "trail1"):
    x = R[R.exit == e]
    print(f"{e}: settings positive overall {int((x.rs > 0).sum())}/{len(x)}, positive in train AND test {int(((x.train > 0) & (x.test > 0)).sum())}/{len(x)}, "
          f"median ₹/trade {x.rs.median():+,.0f}, median test ₹/trade {x.test.median():+,.0f}, median trades {x.n.median():.0f}")
