"""The user's "double confirmation" strategy.

1. Yesterday's high/low (full session).  2. Where today OPENS: above yesterday's high, below its low, or inside.
3. Today's first 15 minutes (09:15-09:30) high/low.  4. Trade the first 5-minute candle (09:30-14:00) that closes
beyond the 15-minute high (buy CE) or low (buy PE). "Double confirmation" = opened above yesterday's high AND broke
the 15-minute high (or opened below yesterday's low AND broke the 15-minute low).
Variants: trigger exactly at the 15-minute high/low, or 0.2x its width beyond (the app's trigger); for days that open
inside yesterday's range, also "wait until it is beyond BOTH the 15-minute AND yesterday's high/low".
Stop at the far side of the first 15 minutes; exits hold (stop or 15:00), trailing after +1R, 2R target.
NIFTY 2015-2026, SENSEX 2018-2026, NIFTY BANK Oct 2022-2026 (monthly options, lot 30) with option P&L as lab.py;
the app's current opening-range rule for comparison.
"""
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
from app import odds  # noqa: E402
from pdhl_test import daily_full  # noqa: E402

lab.META["BANKNIFTY"] = dict(lot=30, step=100.0, txn=0.03503)
lab.START["BANKNIFTY"] = date(2022, 10, 1)


def signal(a, ph, pl, buf, both):
    o, h, l, c = a.T
    H, L = h[:3].max(), l[:3].min()
    w = H - L
    if w <= 0:
        return None
    up, dn = H + buf * w, L - buf * w
    if both:
        up, dn = max(up, ph), min(dn, pl)
    for i in range(3, lab.LAST_SIG + 1):
        if c[i] > up:
            return i, 1, L
        if c[i] < dn:
            return i, -1, H
    return None


def main():
    vix = lab.load_vix()
    rows = []
    for key in ("NIFTY", "SENSEX", "BANKNIFTY"):
        days, ind, fac = lab.prepare(key)
        dl = daily_full(key)
        dds = list(dl.index); pos = {d: k for k, d in enumerate(dds)}
        for d in sorted(days):
            if d < lab.START[key] or "atr" not in fac[d] or d not in pos or pos[d] < 1:
                continue
            y = dl.loc[dds[pos[d] - 1]]
            ph, pl = float(y.h), float(y.l)
            a, F = days[d], fac[d]
            op = a[0, 0]
            where = "above" if op > ph else ("below" if op < pl else "inside")
            vx = vix.get(d)
            exp = odds.weekly_expiry_for(key, d)
            ivk = 1.15 if key == "BANKNIFTY" else 1.0
            for buf, both, tag in ((0.0, False, "trigger at the 15-min high/low"), (0.2, False, "trigger 0.2x beyond (app)"),
                                   (0.0, True, "beyond BOTH 15-min and yesterday's high/low")):
                s = signal(a, ph, pl, buf, both)
                if not s:
                    continue
                i, sd, stop = s
                ex, risk = lab.exits(a, i, sd, stop)
                if ex is None:
                    continue
                agree = (where == "above" and sd == 1) or (where == "below" and sd == -1)
                against = (where == "above" and sd == -1) or (where == "below" and sd == 1)
                grp = "DOUBLE CONFIRMATION (gap + same-way break)" if agree else (
                    "gap, but broke the other way" if against else "opened inside, broke " + ("up" if sd == 1 else "down"))
                app_ok = F.get("yra") is not None and F["yra"] <= 1.2 and F.get("ydir") == sd
                iv = ivk * float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100
                for exn in ("hold", "trail1", "t2"):
                    px, jj, _ = ex[exn]
                    rows.append((key, d, tag, exn, sd, float(a[i, 3]), (i + 1) * 5, iv, exp, (px - a[i, 3]) * sd / risk,
                                 risk, i, float(px), (jj + 1) * 5, 1.0, where, grp, app_ok, F.get("ydir") == sd,
                                 F.get("yra") is not None and F["yra"] > 1.2))
        print(key, len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS + ["open_at", "group", "app_ok", "yday_same", "after_wide"])
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m).astype("float32")
    T.to_pickle("/home/claude/ml/lab/orb_pdhl.pkl")
    print("saved", T.shape)


if __name__ == "__main__":
    main()
