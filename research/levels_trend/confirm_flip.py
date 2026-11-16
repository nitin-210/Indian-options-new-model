"""The user's confirmation idea, checked directly.

For every first 15-minute range break (09:30-14:00, trigger 0.2x the range beyond, stop at the far side):
  CONFIRMED   = at the break the price is already beyond yesterday's high (CE) / low (PE)
  UNCONFIRMED = the break happens while price is still inside yesterday's range
How often each fails (hits its stop before 15:00) and what it earns; the same split for the app's own trades.
The FLIP: after a first break, price closes beyond the OTHER side of the 15-minute range (+0.2x) before 14:00 ->
trade the new direction, stop at the failed break's extreme, stop or 15:00 (and trailing). Split by: was the first
break unconfirmed (inside yesterday's range)? was the flip WITH the bigger trend (20-day trend / yesterday's candle)?
NIFTY 2015-2026, SENSEX 2018-2026, NIFTY BANK Oct 2022-2026, option P&L as lab.py. "Yesterday" = the last regular
session (a Muhurat hour is not used).
"""
import sys
import numpy as np
import pandas as pd
sys.path.insert(0, "/home/claude/ml/lab")
import lab  # noqa: E402
import orb_pdhl  # noqa: E402,F401  (adds NIFTY BANK to lab.META)
from app import odds  # noqa: E402
from pdhl_test import daily_full  # noqa: E402


def main():
    vix = lab.load_vix()
    rows = []
    for key in ("NIFTY", "SENSEX", "BANKNIFTY"):
        days, ind, fac = lab.prepare(key)
        dl = daily_full(key); dds = list(dl.index); pos = {d: k for k, d in enumerate(dds)}
        for d in sorted(days):
            F = fac[d]
            if d < lab.START[key] or "atr" not in F or d not in pos or pos[d] < 1:
                continue
            y = dl.loc[dds[pos[d] - 1]]; ph, pl = float(y.h), float(y.l)
            a = days[d]; o, h, l, c = a.T
            H, L = h[:3].max(), l[:3].min(); w = H - L
            if w <= 0:
                continue
            up, dn = H + 0.2 * w, L - 0.2 * w
            b = next((i for i in range(3, lab.LAST_SIG + 1) if c[i] > up or c[i] < dn), None)
            if b is None:
                continue
            sd = 1 if c[b] > up else -1
            confirmed = (c[b] > ph) if sd == 1 else (c[b] < pl)
            trend20, ydir = F.get("dma20"), F.get("ydir")
            app_ok = F.get("yra") is not None and F["yra"] <= 1.2 and ydir == sd
            vx = vix.get(d); exp = odds.weekly_expiry_for(key, d)
            ivk = 1.15 if key == "BANKNIFTY" else 1.0
            iv = lambda i: ivk * float(vx[i] if vx is not None and np.isfinite(vx[i]) else 14.0) / 100

            def add(kind, i, s, stop, extra):
                ex, risk = lab.exits(a, i, s, stop)
                if ex is None:
                    return None
                for exn in ("hold", "trail1"):
                    px, jj, _ = ex[exn]
                    rows.append((key, d, kind, exn, s, float(c[i]), (i + 1) * 5, iv(i), exp, (px - c[i]) * s / risk, risk, i,
                                 float(px), (jj + 1) * 5, 1.0, *extra))
                return ex
            ex = add("first break", b, sd, L if sd == 1 else H,
                     (confirmed, app_ok, trend20 == sd, ydir == sd, False))
            # the flip: a close beyond the other side after the first break
            f = next((i for i in range(b + 1, lab.LAST_SIG + 1) if ((c[i] < dn) if sd == 1 else (c[i] > up))), None)
            if f is not None:
                s2 = -sd
                stop2 = h[b:f + 1].max() if s2 == -1 else l[b:f + 1].min()
                add("flip", f, s2, stop2, (confirmed, app_ok, trend20 == s2, ydir == s2, True))
        print(key, len(rows), flush=True)
    T = pd.DataFrame(rows, columns=lab.COLS + ["first_confirmed", "app_ok", "with_trend20", "with_yday", "is_flip"])
    for m in ("cal", "mkt"):
        T[f"pnl_{m}"] = lab.opt_pnl(T, m).astype("float32")
    T.to_pickle("/home/claude/ml/lab/confirm_flip.pkl")


def st(x, lbl, nm):
    cut = pd.Timestamp("2022-02-23").date() if nm != "BANKNIFTY" else pd.Timestamp("2025-02-01").date()
    M = pd.period_range("2022-10" if nm == "BANKNIFTY" else "2015-01", "2026-11", freq="M")
    w = x.pnl_cal > 0
    stop_hit = (x.R <= -0.95).mean()
    print(f"  {lbl:<62} {len(x)/len(M):>4.1f}/mo | stopped out {stop_hit:.0%} | win {10*w.mean():.1f}/10 | ₹/trade {x.pnl_cal.mean():+,.0f} "
          f"(train {x[x.date<cut].pnl_cal.mean():+,.0f} test {x[x.date>=cut].pnl_cal.mean():+,.0f}) | stress ₹/trade {x.pnl_mkt.mean():+,.0f}")


def report():
    T = pd.read_pickle("/home/claude/ml/lab/confirm_flip.pkl")
    for nm, keys in (("NIFTY + SENSEX", ("NIFTY", "SENSEX")), ("BANKNIFTY", ("BANKNIFTY",))):
        X = T[T.key.isin(keys) & (T.exit == "trail1")]
        fb, fl = X[X.strat == "first break"], X[X.strat == "flip"]
        print(f"\n===== {nm} (trailing exit)")
        print(" first 15-minute breaks:")
        st(fb[fb.first_confirmed], "CONFIRMED (already beyond yesterday's high/low)", nm)
        st(fb[~fb.first_confirmed], "UNCONFIRMED (still inside yesterday's range)", nm)
        print(" the app's own trades (yesterday's direction, no wide day):")
        st(fb[fb.app_ok & fb.first_confirmed], "app trade, confirmed", nm)
        st(fb[fb.app_ok & ~fb.first_confirmed], "app trade, unconfirmed", nm)
        print(f" flips (the first break failed and price broke the other side): {len(fl)/max(len(fb),1):.0%} of days with a break")
        st(fl, "every flip", nm)
        st(fl[~fl.first_confirmed], "flip after an UNCONFIRMED first break", nm)
        st(fl[~fl.first_confirmed & fl.with_trend20], "...and the flip goes WITH the 20-day trend (user's case)", nm)
        st(fl[~fl.first_confirmed & fl.with_yday], "...and the flip goes with yesterday's candle", nm)
        st(fl[fl.first_confirmed], "flip after a CONFIRMED first break", nm)
        hx = T[T.key.isin(keys) & (T.exit == "hold") & (T.strat == "flip") & ~T.first_confirmed & T.with_trend20]
        st(hx, "user's case, held to the stop or 15:00 (no trailing)", nm)


if __name__ == "__main__":
    if "--report" not in sys.argv:
        main()
    report()
