"""The 09:15 long straddle: buy the at-the-money CE and PE (same strike, 1 lot each) at the 09:15 open, sell later.
NIFTY 2015-2026, SENSEX 2018-2026 (weekly options), NIFTY BANK Oct 2022-2026 (monthly options, lot 30).
Option prices: Black-Scholes with India VIX as the volatility AT EACH MOMENT (the 09:15 VIX open at entry, the VIX at
the exit time) - so the usual fall in volatility after the open is counted (NIFTY BANK: VIX x 1.15). 0.5% slippage
on each leg each way plus Indian charges on both legs. "Stress" = all decay inside market hours.
"""
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
import orb_pdhl  # noqa: E402,F401 (NIFTY BANK meta)
from app import odds  # noqa: E402

EXITS = {"10:15": 60, "11:30": 135, "13:00": 225, "15:00": 345, "15:15": 360}


def vix_open():
    rows = json.loads((lab.H / "VIX_5m.json").read_text())
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["d"], df["m"] = ts.dt.date, ts.dt.hour * 60 + ts.dt.minute
    return df[df.m == 555].set_index("d").o.to_dict()


def leg(key, S0, S1, K, T0, T1, iv0, iv1, call):
    m = lab.META[key]
    p0 = lab.bs(S0, K, T0, iv0, call) * 1.005
    p1 = np.maximum(lab.bs(S1, K, T1, iv1, call), 0.05) * 0.995
    q = m["lot"]
    turn = (p0 + p1) * q
    ch = 40 + 0.0015 * p1 * q + m["txn"] / 100 * turn + 1e-6 * turn + 0.00003 * p0 * q
    ch += 0.18 * (40 + m["txn"] / 100 * turn + 1e-6 * turn)
    return q * (p1 - p0) - ch, q * p0


def main():
    vix, vo = lab.load_vix(), vix_open()
    rows = []
    for key in ("NIFTY", "SENSEX", "BANKNIFTY"):
        days, ind, fac = lab.prepare(key)
        k_iv = 1.15 if key == "BANKNIFTY" else 1.0
        for d in sorted(days):
            if d < lab.START[key] or d not in vix or d not in vo:
                continue
            a = days[d]
            S0 = a[0, 0]
            K = round(S0 / lab.META[key]["step"]) * lab.META[key]["step"]
            exp = odds.weekly_expiry_for(key, d)
            iv0 = k_iv * vo[d] / 100
            for nm, m1 in EXITS.items():
                i = min(m1 // 5 - 1, 71)
                S1 = a[i, 3]
                iv1 = k_iv * vix[d][i] / 100
                for mode in ("cal", "mkt"):
                    T0 = lab.t_years(np.array([d]), np.array([0]), np.array([exp]), mode)[0]
                    T1 = lab.t_years(np.array([d]), np.array([m1]), np.array([exp]), mode)[0]
                    pc, cc = leg(key, S0, S1, K, T0, T1, iv0, iv1, True)
                    pp, cp = leg(key, S0, S1, K, T0, T1, iv0, iv1, False)
                    rows.append((key, d, nm, mode, pc + pp, cc + cp, abs(S1 - S0) / S0 * 100, (iv1 - iv0) * 100 / k_iv))
        print(key, len(rows), flush=True)
    T = pd.DataFrame(rows, columns=["key", "date", "exit", "mode", "pnl", "cost", "move_pct", "vix_chg"])
    T.to_pickle("/home/claude/ml/lab/straddle_bt.pkl")
    C = T[T["mode"] == "cal"]
    S = T[T["mode"] == "mkt"].set_index(["key", "date", "exit"]).pnl
    for key, g in C.groupby("key"):
        nm = {"NIFTY": "NIFTY 50", "SENSEX": "SENSEX", "BANKNIFTY": "NIFTY BANK"}[key]
        print(f"\n{nm} ({g.date.min()} -> {g.date.max()}, {g.date.nunique()} days); costs about ₹{g.cost.median():,.0f} a straddle")
        for ex in EXITS:
            x = g[g.exit == ex]
            yr = x.groupby(pd.to_datetime(x.date).dt.year).pnl.sum()
            st = S.loc[[(key, d, ex) for d in x.date]].mean()
            print(f"  sell at {ex}: days in profit {np.mean(x.pnl > 0):.0%} | avg ₹{x.pnl.mean():+,.0f} a day "
                  f"(avg win ₹{x.pnl[x.pnl > 0].mean():,.0f}, avg loss ₹{-x.pnl[x.pnl <= 0].mean():,.0f}) | "
                  f"₹{x.pnl.sum() / max(1, x.date.nunique()) * 21:+,.0f} a month | losing years {int((yr < 0).sum())} of {len(yr)} | stress ₹{st:+,.0f} a day")
    x = C[C.exit == "15:15"]
    print(f"\nall three, sell at 15:15: India VIX change 09:15 -> 15:15 averaged {x.vix_chg.mean():+.2f} points; "
          f"the index moved {x.move_pct.median():.2f}% (median) from the open")
    big = x[x.move_pct >= x.move_pct.quantile(0.8)]
    print(f"  on the 20% of days with the biggest moves the straddle made ₹{big.pnl.mean():+,.0f} on average; "
          f"on the other 80%: ₹{x[x.move_pct < x.move_pct.quantile(0.8)].pnl.mean():+,.0f}")


if __name__ == "__main__":
    main()
