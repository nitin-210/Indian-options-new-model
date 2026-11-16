"""Combine with our model: (1) filters on our signals from the other strategies' state at the signal, (2) a portfolio of
our model + the strategies that held up, (3) exit changes. Chosen on the train years (Jan 2015 - Feb 2022) only."""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402

df = pd.read_pickle("/home/claude/ml/lab/lab_trades.pkl")
df["d"] = pd.to_datetime(df["date"])
ds = np.sort(df.d.unique()); cut = ds[int(len(ds) * 0.6)]
df["part"] = np.where(df.d < cut, "train", "test")
M = {p: df[df.part == p].d.dt.to_period("M").nunique() for p in ("train", "test")}
OURS = "OUR MODEL (ORB15, yday, no wide day)"


def line(x, label):
    out = []
    for p in ("train", "test"):
        y = x[x.part == p]
        if not len(y):
            out.append(f"{p} -"); continue
        day = y.groupby("d")[["pnl_cal", "pnl_mkt"]].sum()
        mon = y.groupby(y.d.dt.to_period("M")).pnl_cal.sum()
        w = y.pnl_cal > 0
        out.append(f"{p} {len(y):>4} tr win {w.mean():>3.0%} avg win ₹{y.pnl_cal[w].mean():>6,.0f} loss ₹{y.pnl_cal[~w].mean():>7,.0f} "
                   f"₹{y.pnl_cal.sum() / M[p]:>+7,.0f}/mo (stress ₹{y.pnl_mkt.sum() / M[p]:>+7,.0f}) green {np.mean(day.pnl_cal > 0):>3.0%} "
                   f"worst mo ₹{mon.min():>+7,.0f}")
    print(f"  {label:<46} " + " || ".join(out))


# ---------------- (1) filters on our signals: state of the other tools when our signal candle closes
our = df[(df.strat == OURS)].copy()
feats = []
for key in ("NIFTY", "SENSEX"):
    days, ind, fac = lab.prepare(key)
    for r in our[(our.key == key) & (our.exit == "hold")].itertuples():
        a, I, F = days[r.date], ind[r.date], fac[r.date]
        i, sd = int(r.t_in), int(r.sd)
        tw = lab.twap(a)
        last15 = i - ((i - 2) % 3)                     # the latest completed 15-minute candle at this moment
        feats.append({"key": key, "date": r.date,
                      "st15": int(I["dir15"][last15]) == sd if last15 >= 2 else None,
                      "ema15": (I["e9_15"][last15] > I["e21_15"][last15]) == (sd == 1) if last15 >= 2 else None,
                      "ema5": (I["e9"][i] > I["e21"][i]) == (sd == 1),
                      "twap": (a[i, 3] > tw[i]) == (sd == 1),
                      "dma20": F.get("dma20") == sd,
                      "gap": ((a[0, 0] - F["pc"]) * sd > 0) if "pc" in F else None,
                      "early": i <= 8,                     # signal by 10:00
                      "or_narrow": (a[:3, 1].max() - a[:3, 2].min()) / F["atr"] < 0.35,
                      "rsi_ok": (I["rsi"][i] < 75) if sd == 1 else (I["rsi"][i] > 25)})
FE = pd.DataFrame(feats)
our = our.merge(FE, on=["key", "date"], how="left")
# both indices signalled the same side today
both = our[our.exit == "hold"].groupby("date").agg(n=("sd", "size"), same=("sd", lambda s: s.nunique() == 1))
our["both_same"] = our.date.map(lambda d: bool(both.loc[d, "n"] == 2 and both.loc[d, "same"]) if d in both.index else False)
print("(1) OUR MODEL with filters (exit: hold to stop / 15:00). Current pricing; stress = all decay in market hours")
base = our[our.exit == "hold"]
line(base, "our model as it is")
for f, lab_ in [("st15", "Supertrend 15m agrees"), ("ema15", "EMA 9/21 15m agrees"), ("ema5", "EMA 9/21 5m agrees"),
                ("twap", "price on the right side of TWAP"), ("dma20", "20-day trend agrees"), ("gap", "opening gap agrees"),
                ("early", "signal by 10:00"), ("or_narrow", "narrow opening range (<0.35 ATR)"), ("rsi_ok", "not overbought/oversold"),
                ("both_same", "NIFTY and SENSEX same side")]:
    line(base[base[f] == True], "+ " + lab_)       # noqa: E712
    line(base[base[f] == False], "  (the rest)")    # noqa: E712

# ---------------- (2) exits for our model
print("\n(2) OUR MODEL, different exits")
for e, lab_ in [("hold", "hold to stop / 15:00 (now)"), ("trail1", "after +1R, trail the stop 1R behind"),
                ("be1", "after +1R, stop to entry"), ("half1", "half at +1R, rest stop to entry"),
                ("t2", "take profit at 2R"), ("t15", "take profit at 1.5R"), ("t1", "take profit at 1R")]:
    line(our[our.exit == e], lab_)

# ---------------- (3) portfolio: our model + others that held up
print("\n(3) PORTFOLIO: our model + other strategies on the same days (each its own 1-lot trade)")
pick = {"ours": (OURS, "hold"), "ORB60": ("ORB60", "hold"), "Supertrend15": ("Supertrend 15m flip", "hold"),
        "EMA15+yday": ("EMA 9/21 cross 15m + yday", "hold"), "afternoon": ("afternoon break (13:00 range)", "hold"),
        "ORB30+yday": ("ORB30 + yday", "hold")}
P = {k: df[(df.strat == s) & (df.exit == e)] for k, (s, e) in pick.items()}
for combo in (["ours"], ["ours", "Supertrend15"], ["ours", "ORB60"], ["ours", "afternoon"], ["ours", "EMA15+yday"],
              ["ours", "Supertrend15", "afternoon"], ["ours", "ORB60", "Supertrend15", "afternoon"]):
    line(pd.concat([P[c] for c in combo]), " + ".join(combo))
