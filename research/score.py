import numpy as np, pandas as pd
df = pd.read_pickle("lab_trades.pkl")
df["d"] = pd.to_datetime(df["date"])
ds = np.sort(df.d.unique()); cut = ds[int(len(ds) * 0.6)]
df["part"] = np.where(df.d < cut, "train", "test")
months = {p: df[df.part == p].d.dt.to_period("M").nunique() for p in ("train", "test")}
def stats(g, part):
    x = g[g.part == part]
    if len(x) == 0:
        return {}
    day = x.groupby("d")[["pnl_cal", "pnl_mkt"]].sum()
    mon = x.groupby(x.d.dt.to_period("M"))[["pnl_cal", "pnl_mkt"]].sum()
    w = x.pnl_cal > 0
    return {"n": len(x), "per_mo": len(x) / months[part],
            "win_cal": w.mean(), "win_mkt": (x.pnl_mkt > 0).mean(), "winR": (x.R > 0).mean(),
            "avgwin": x.pnl_cal[w].mean(), "avgloss": x.pnl_cal[~w].mean(),
            "mo_cal": x.pnl_cal.sum() / months[part], "mo_mkt": x.pnl_mkt.sum() / months[part],
            "green_cal": (day.pnl_cal > 0).mean(), "green_mkt": (day.pnl_mkt > 0).mean(),
            "worst_mo": mon.pnl_cal.min(), "red_mo": (mon.pnl_cal < 0).mean(), "R": x.R.mean()}
rows = []
for (s, e), g in df.groupby(["strat", "exit"], observed=True):
    tr, te = stats(g, "train"), stats(g, "test")
    if tr and te:
        rows.append({"strat": s, "exit": e, **{f"tr_{k}": v for k, v in tr.items()}, **{f"te_{k}": v for k, v in te.items()}})
S = pd.DataFrame(rows)
S.to_pickle("scores.pkl")
print(f"train {df.d.min():%b %Y} - {pd.Timestamp(cut):%b %Y} ({months['train']} mo), test to {df.d.max():%b %Y} ({months['test']} mo); "
      f"{S.strat.nunique()} strategies x {S.exit.nunique()} exits = {len(S)} combos")
fmt = lambda r, p: (f"{r[p+'n']:>5.0f} ({r[p+'per_mo']:>4.1f}/mo) win {r[p+'win_cal']:>3.0%}/{r[p+'win_mkt']:>3.0%} "
                    f"₹{r[p+'mo_cal']:>+8,.0f}/{r[p+'mo_mkt']:>+8,.0f} green {r[p+'green_cal']:>3.0%} worst {r[p+'worst_mo']:>+8,.0f}")
pd.set_option("display.width", 250)
print("\nOUR MODEL, every exit   [trades | win% cal/mkt | ₹ per month cal/mkt | green days | worst month]")
for _, r in S[S.strat.str.startswith("OUR")].iterrows():
    print(f"  {r.exit:<7} TRAIN {fmt(r, 'tr_')} || TEST {fmt(r, 'te_')}")
rob = S[(S.tr_mo_cal > 0) & (S.tr_mo_mkt > 0) & (S.tr_n >= 150)]
print(f"\nSURVIVORS: profitable in the train years under BOTH pricings: {len(rob)} of {len(S)} combos")
for _, r in rob.sort_values("te_mo_mkt", ascending=False).iterrows():
    print(f"  {r.strat[:44]:<44} {r.exit:<6} TRAIN {fmt(r, 'tr_')} || TEST {fmt(r, 'te_')}")
mild = S[(S.tr_mo_cal > 0) & (S.tr_n >= 150)]
print(f"\nProfitable in train with the CURRENT pricing only: {len(mild)} combos; top 25 by test ₹ (current pricing):")
for _, r in mild.sort_values("te_mo_cal", ascending=False).head(25).iterrows():
    print(f"  {r.strat[:44]:<44} {r.exit:<6} TRAIN {fmt(r, 'tr_')} || TEST {fmt(r, 'te_')}")
