"""Build 1-minute day arrays (375 bars, bar-start 09:15 .. 15:29) for NIFTY (2011 - Feb 2023), SENSEX (Mar 2018 -
Mar 2024) and F&O stocks (2017 - 2021), and check them against the 5-minute history the app's research used.

Sources (already on disk): /tmp/NIFTY_1m.pkl (bar-END labels: 09:16 = the 09:15 minute), the Sensex minute CSV
(bar-start labels), the aeron7 1-minute repository for stocks (bar-END labels).
Days with fewer than 360 of the 375 minutes are dropped (Muhurat and broken days); single missing minutes are filled
flat from the previous close.
"""
import glob
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

OUT = "/home/claude/ml/m1/data"
os.makedirs(OUT, exist_ok=True)
NB = 375


def to_days(df, shift):
    """df with columns d (date), m (minute of day, bar START after shift), o h l c v -> {date: (375, 5) array}"""
    df = df[(df.m >= 555) & (df.m < 930)]
    out = {}
    for d, g in df.groupby("d", sort=True):
        if len(g) < 360:
            continue
        a = np.full((NB, 5), np.nan)
        idx = (g.m.to_numpy() - 555).astype(int)
        a[idx] = g[["o", "h", "l", "c", "v"]].to_numpy(float)
        if np.isnan(a[0, 3]):
            first = np.flatnonzero(~np.isnan(a[:, 3]))[0]
            a[:first] = a[first]
        for k in range(1, NB):
            if np.isnan(a[k, 3]):
                c = a[k - 1, 3]
                a[k] = (c, c, c, c, 0.0)
        a[np.isnan(a[:, 4]), 4] = 0.0
        out[d] = a
    return out


def save(key, days):
    ds = sorted(days)
    np.savez_compressed(f"{OUT}/{key}.npz", dates=np.array([d.isoformat() for d in ds]),
                        bars=np.stack([days[d] for d in ds]).astype(np.float32))
    print(f"{key}: {len(ds)} days {ds[0]} -> {ds[-1]}", flush=True)


def check(key, days):
    """Compare 5-minute candles built from these minutes with the research 5-minute history."""
    rows = json.loads(open(f"/home/claude/bigdata/history/{key}_5m.json").read())
    h = {}
    for ts, o, hi, lo, c, v in rows:
        d = date.fromisoformat(ts[:10])
        mm = int(ts[11:13]) * 60 + int(ts[14:16])
        if 555 <= mm < 930:
            h[(d, mm)] = (o, hi, lo, c)
    n = ok = 0
    for d, a in days.items():
        for k in range(0, NB, 5):
            r = h.get((d, 555 + k))
            if r is None:
                continue
            b = a[k:k + 5]
            mine = (b[0, 0], b[:, 1].max(), b[:, 2].min(), b[-1, 3])
            n += 1
            ok += all(abs(x - y) <= max(0.06, abs(y) * 2e-5) for x, y in zip(mine, r))
    print(f"  {key}: {n:,} overlapping 5-minute candles, {ok / max(n, 1):.1%} identical", flush=True)


def nifty():
    df = pd.read_pickle("/tmp/NIFTY_1m.pkl")
    df["d"] = pd.to_datetime(df.d, format="%Y%m%d").dt.date
    df["m"] = df.t.str[:2].astype(int) * 60 + df.t.str[3:5].astype(int) - 1       # bar-end -> bar-start
    df = df[df.d >= date(2011, 1, 1)]
    days = to_days(df, 1)
    check("NIFTY", days)
    save("NIFTY", days)


def sensex():
    s = pd.read_csv("/home/claude/sandeepkapri/sensex-minute-data/sensex_candlestick_data.csv")
    ts = pd.to_datetime(s.Date + " " + s.Time, format="%d-%m-%Y %H:%M:%S")
    df = pd.DataFrame({"d": ts.dt.date, "m": ts.dt.hour * 60 + ts.dt.minute, "o": s.Open, "h": s.High,
                       "l": s.Low, "c": s.Close, "v": 0.0})
    days = to_days(df, 0)
    check("SENSEX", days)
    save("SENSEX", days)


def stocks():
    root = "/home/claude/aeron7/nifty-banknifty-intraday-data"
    files = [f for y in ("2017", "2018", "2019", "2020", "2021") for f in glob.glob(f"{root}/{y}/*/*/*/*.txt")]
    skip = ("NIFTY", "BANKNIFTY", "INDIAVIX", "CNX", "FINNIFTY")
    by = {}
    for f in files:
        name = os.path.basename(f)[:-4]
        if "_F" in name or name.startswith(skip) or "[" in name:
            continue
        by.setdefault(name, []).append(f)
    names = sorted(n for n, fs in by.items() if len(fs) >= 900)
    print(f"stocks with >= 900 days of minutes: {len(names)}", flush=True)
    for name in names:
        parts = []
        for f in by[name]:
            try:
                parts.append(pd.read_csv(f, header=None, usecols=range(8),
                                         names=["s", "d", "t", "o", "h", "l", "c", "v"], dtype={"d": str, "t": str}))
            except Exception:
                continue
        df = pd.concat(parts, ignore_index=True)
        df["d"] = pd.to_datetime(df.d, format="%Y%m%d", errors="coerce").dt.date
        df = df.dropna(subset=["d"])
        df["m"] = df.t.str[:2].astype(int) * 60 + df.t.str[3:5].astype(int) - 1
        df = df.drop_duplicates(["d", "m"], keep="last")
        days = to_days(df, 1)
        if len(days) >= 800:
            ds = sorted(days)
            np.savez_compressed(f"{OUT}/stk_{name}.npz", dates=np.array([d.isoformat() for d in ds]),
                                bars=np.stack([days[d] for d in ds]).astype(np.float32))
    print("stocks saved:", len(glob.glob(f"{OUT}/stk_*.npz")), flush=True)


if __name__ == "__main__":
    which = sys.argv[1:] or ["nifty", "sensex", "stocks"]
    for w in which:
        globals()[w]()
