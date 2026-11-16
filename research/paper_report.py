import numpy as np, pandas as pd
df = pd.read_pickle("paper_strategies.pkl")
df["d"] = pd.to_datetime(df["date"])
ds = np.sort(df.d.unique()); cut = ds[int(len(ds) * 0.6)]
df["part"] = np.where(df.d < cut, "train", "test")
M = {p: df[df.part == p].d.dt.to_period("M").nunique() for p in ("train", "test")}
print(f"Feb 2019 - Nov 2026, NIFTY + SENSEX, 1 lot each. train = to {pd.Timestamp(cut):%b %Y} ({M['train']} months), "
      f"test = after ({M['test']} months). Rupees after slippage and charges.\n")
def table(sub, label):
    print(label)
    print(f"  {'structure':<34} {'trades':>6} | {'train ₹/month':>13} {'win':>4} | {'test ₹/month':>12} {'win':>4} {'worst month':>11} {'avg/trade':>9}")
    for (v, s), g in sub.groupby(["view", "structure"], sort=False):
        if s == "no trade":
            continue
        tr, te = g[g.part == "train"], g[g.part == "test"]
        wm = te.groupby(te.d.dt.to_period("M")).pnl.sum().min()
        print(f"  {(v + ': ' + s) if v not in ('none',) else s:<34} {len(g):>6} | {tr.pnl.sum()/M['train']:>+13,.0f} {np.mean(tr.pnl>0):>4.0%} | "
              f"{te.pnl.sum()/M['test']:>+12,.0f} {np.mean(te.pnl>0):>4.0%} {wm:>+11,.0f} {te.pnl.mean():>+9,.0f}")
    print()
order = lambda x: x.sort_values(["view", "structure"])
table(df[df["mode"] == "intraday"], "A) ON ITS OWN, INTRADAY (enter 09:30, or 10:15 for the first-hour view; out at 15:00)")
table(df[df["mode"] == "to expiry"], "B) ON ITS OWN, THE PAPER'S WAY (enter 09:30 on the first day of each weekly expiry, held to expiry)")
table(df[df["mode"] == "our signal"], "C) WITH OUR MODEL: the same signals, entry, stop and 15:00 exit, different option structure")
# D: neutral structures on the days our model does not trade
ours = df[df["mode"] == "our signal"]
no = set(zip(ours[ours.structure == "no trade"].key, ours[ours.structure == "no trade"].date))
yes = set(zip(ours[ours.structure != "no trade"].key, ours[ours.structure != "no trade"].date))
neu = df[(df["mode"] == "intraday") & (df.view == "none")].copy()
neu["ours"] = [("no" if (k, d) in no else "yes" if (k, d) in yes else "?") for k, d in zip(neu.key, neu.date)]
table(neu[neu.ours == "no"].assign(view="none"), "D) NEUTRAL STRUCTURES ONLY ON THE DAYS OUR MODEL DOES NOT TRADE")
table(neu[neu.ours == "yes"].assign(view="none"), "   ...and on the days it does trade (for comparison)")
# by year for the main candidates
print("BY YEAR (₹, NIFTY + SENSEX, 1 lot each)")
pick = {"our model: long option": (df["mode"] == "our signal") & (df.structure == "long option"),
        "our model: debit spread": (df["mode"] == "our signal") & (df.structure == "debit spread"),
        "long straddle (intraday)": (df["mode"] == "intraday") & (df.structure == "long straddle"),
        "short straddle (intraday)": (df["mode"] == "intraday") & (df.structure == "short straddle"),
        "long butterfly (intraday)": (df["mode"] == "intraday") & (df.structure == "long butterfly"),
        "20-day trend: debit spread (intraday)": (df["mode"] == "intraday") & (df.view == "20-day trend") & (df.structure == "debit spread"),
        "first hour: long option": (df["mode"] == "intraday") & (df.view == "first hour") & (df.structure == "long option"),
        "short straddle (to expiry)": (df["mode"] == "to expiry") & (df.structure == "short straddle")}
yr = pd.DataFrame({k: df[m].groupby(df[m].d.dt.year).pnl.sum() for k, m in pick.items()}).T
print(yr.round(-2).astype(int).to_string())
