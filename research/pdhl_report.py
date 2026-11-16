"""Report for pdhl_test.py: the previous-day breakout alone, then with the app's opening-range rule."""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
T = pd.read_pickle("/home/claude/ml/lab/pdhl_trades.pkl")
C = pd.read_pickle("/home/claude/ml/lab/pdhl_ctx.pkl")
A = pd.read_pickle("/home/claude/ml/lab/app_rule.pkl")
L = pd.read_pickle("/home/claude/ml/lab/lab_trades.pkl")
ST = L[(L.strat == "Supertrend 15m flip") & (L.exit == "hold")].copy()
ORB = A[A.exit == "trail1"].copy()
alld = sorted(set(C.date))
CUT = alld[int(len(alld) * 0.6)]
ALLM = pd.period_range("2015-01", "2026-11", freq="M")


def stats(x, label, show=True):
    x = x.sort_values("date")
    d = pd.to_datetime(x.date)
    mon = x.groupby(d.dt.to_period("M")).pnl_cal.sum().reindex(ALLM, fill_value=0)
    yr = x.groupby(d.dt.year).pnl_cal.sum()
    w = x.pnl_cal > 0
    tr, te = x[x.date < CUT], x[x.date >= CUT]
    out = dict(label=label, per_mo=len(x) / len(ALLM), win10=10 * w.mean(),
               avg_win=x.pnl_cal[w].mean(), avg_loss=-x.pnl_cal[~w].mean(), per_trade=x.pnl_cal.mean(),
               train=tr.pnl_cal.mean(), test=te.pnl_cal.mean(), R_tr=tr.R.mean(), R_te=te.R.mean(),
               green_mo=np.mean(mon > 0), rs_mo=mon.mean(), last3=mon[-35:].mean(), worst_mo=mon.min(),
               lose_yrs=int((yr < 0).sum()), stress=x.pnl_mkt.sum() / len(ALLM))
    if show:
        print(f"{label:<52} {out['per_mo']:>5.1f}/mo | of 10: {out['win10']:.1f} win | avg win ₹{out['avg_win']:,.0f} "
              f"loss ₹{out['avg_loss']:,.0f} | ₹/trade {out['per_trade']:+,.0f} (train {out['train']:+,.0f} test "
              f"{out['test']:+,.0f}) | green mo {out['green_mo']:.0%} | ₹{out['rs_mo']:+,.0f}/mo (last 3y "
              f"₹{out['last3']:+,.0f}) | worst mo ₹{out['worst_mo']:+,.0f} | losing yrs {out['lose_yrs']} | "
              f"stress ₹{out['stress']:+,.0f}/mo")
    return out


print("=" * 40, "1. PREVIOUS-DAY BREAKOUT ALONE (all variants, ranked on the TRAIN years only)")
res = []
for (s, e), x in T.groupby(["strat", "exit"], observed=True):
    r = stats(x, f"{s} {e}", show=False)
    res.append(r)
R = pd.DataFrame(res).sort_values("train", ascending=False)
pd.set_option("display.width", 250)
print(R[["label", "per_mo", "win10", "avg_win", "avg_loss", "train", "test", "R_tr", "R_te", "rs_mo", "green_mo",
         "lose_yrs", "stress"]].head(12).round(2).to_string(index=False))
print("...worst 5:")
print(R[["label", "per_mo", "train", "test", "rs_mo", "stress"]].tail(5).round(1).to_string(index=False))
print(f"\nvariants positive in BOTH train and test (option P&L, app model): {int(((R.train > 0) & (R.test > 0)).sum())} of {len(R)}; "
      f"positive under the stress decay model: {int((R.stress > 0).sum())}")

best = R.iloc[0]["label"]
bs, be = best.rsplit(" ", 1)
P = T[(T.strat == bs) & (T.exit == be)].copy()
print("\nchosen on train:", best)
stats(P, "PDH/PDL breakout (chosen)")
for y, v in P.groupby(pd.to_datetime(P.date).dt.year).pnl_cal.sum().items():
    print(f"   {y}: ₹{v:+,.0f}", end="")
print()

# simple, untuned version for reference: 5-minute close beyond, from the open, stop back inside, trailing exit
SIMPLE = T[(T.strat == "5m|open|any|back") & (T.exit == "trail1")]
stats(SIMPLE, "PDH/PDL simple (5m, from open, stop back inside, trail)")

print("\n" + "=" * 40, "2. WITH THE OPENING-RANGE RULE")
base = stats(ORB, "Opening-range rule (app, trailing exit)")
stats(ST, "Supertrend 15m flip")
stats(pd.concat([ORB, ST]), "CURRENT SYSTEM: opening range + Supertrend")

# where was price against yesterday's range at the opening-range signal?
M = ORB.merge(C, on=["key", "date"], how="left")
beyond = ((M.sd == 1) & (M.S0 > M.ph)) | ((M.sd == -1) & (M.S0 < M.pl))
room = np.where(M.sd == 1, (M.ph - M.S0), (M.S0 - M.pl)) / M.risk        # distance to yesterday's high/low in R
print("\nOpening-range trades split by yesterday's high/low at the signal:")
stats(M[beyond], "  A. already beyond yesterday's high (CE) / low (PE)")
stats(M[~beyond & (room < 1)], "  B. inside, yesterday's high/low less than 1R away")
stats(M[~beyond & (room >= 1)], "  C. inside, 1R or more of room")
stats(M[M.bias == M.sd], "  D. yesterday CLOSED beyond the day before's range, same way")
stats(M[(M.bias != M.sd)], "  E. no such close (or the other way)")

# daily-close bias as the direction instead of yesterday's candle colour: re-run the opening-range break
sys.path.insert(0, "/home/claude/build/india-options")
import lab  # noqa: E402
from app import odds  # noqa: E402
vix = lab.load_vix()
cmap = {(r.key, r.date): r for r in C.itertuples()}
rows = []
for key in ("NIFTY", "SENSEX"):
    days, ind, fac = lab.prepare(key)
    for d in sorted(days):
        r = cmap.get((key, d))
        if r is None or r.bias == 0 or fac[d].get("yra") is None or fac[d]["yra"] > 1.2:
            continue
        a = days[d]
        o, h, l, c = a.T
        H, Lo = h[:3].max(), l[:3].min()
        w = H - Lo
        if w <= 0:
            continue
        sd = int(r.bias)
        i = next((i for i in range(3, lab.LAST_SIG + 1) if ((c[i] > H + 0.2 * w) if sd == 1 else (c[i] < Lo - 0.2 * w))), None)
        if i is None:
            continue
        ex, risk = lab.exits(a, i, sd, Lo if sd == 1 else H)
        if ex is None:
            continue
        px, j, _ = ex["trail1"]
        vx = vix.get(d)
        iv = float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
        rows.append((key, d, "ORB daily-close bias", "trail1", sd, float(c[i]), (i + 1) * 5, iv,
                     odds.weekly_expiry_for(key, d), (px - c[i]) * sd / risk, risk, i, float(px), (j + 1) * 5, 1.0))
B = pd.DataFrame(rows, columns=lab.COLS)
for m in ("cal", "mkt"):
    B[f"pnl_{m}"] = lab.opt_pnl(B, m)
print()
stats(B, "Opening range, direction = yesterday closed beyond range")

# as an extra trade next to the current system
print()
P2 = P.copy()
stats(pd.concat([ORB, ST, P2]), "Current system + PDH/PDL (chosen) as a 3rd trade")
stats(pd.concat([ORB, ST, SIMPLE]), "Current system + PDH/PDL (simple) as a 3rd trade")
orb_days = set(zip(ORB.key, ORB.date))
P3 = P2[[(k, d) not in orb_days for k, d in zip(P2.key, P2.date)]]
stats(pd.concat([ORB, ST, P3]), "Current system + PDH/PDL only on days with no ORB trade")
keepA = M[~(~beyond & (room < 1))]
stats(pd.concat([keepA.drop(columns=["ph", "pl", "atr", "bias", "open"]), ST]),
      "Current system, skipping ORB trades blocked by yday high/low (B)")
pd.to_pickle({"P": P, "B": B}, "/home/claude/ml/lab/pdhl_sets.pkl")
