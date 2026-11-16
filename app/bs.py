"""Black-Scholes pricing, implied volatility and Greeks for European index options.

NIFTY and SENSEX options are European and cash-settled. Time is measured in
calendar years to 15:30 IST on expiry day. Results are estimates: real
premiums also move with demand, skew and the futures basis.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from .timeutil import IST, SESSION_CLOSE

YEAR_S = 365.0 * 86400.0
MIN_T = 60.0 / YEAR_S  # one minute


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years_to_expiry(now: datetime, expiry: date) -> float:
    exp_dt = datetime.combine(expiry, SESSION_CLOSE, tzinfo=IST)
    return max((exp_dt - now).total_seconds() / YEAR_S, MIN_T)


def _d1d2(S: float, K: float, T: float, r: float, sigma: float):
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    return d1, d1 - sq


def price(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    T = max(T, MIN_T)
    sigma = max(sigma, 1e-4)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    disc = math.exp(-r * T)
    if kind == "CE":
        return S * norm_cdf(d1) - K * disc * norm_cdf(d2)
    return K * disc * norm_cdf(-d2) - S * norm_cdf(-d1)


def delta(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    T = max(T, MIN_T)
    d1, _ = _d1d2(S, K, T, r, max(sigma, 1e-4))
    return norm_cdf(d1) if kind == "CE" else norm_cdf(d1) - 1.0


def theta_per_day(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    """Premium change per calendar day from time decay alone (negative for a buyer)."""
    T = max(T, MIN_T)
    sigma = max(sigma, 1e-4)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    first = -S * norm_pdf(d1) * sigma / (2.0 * math.sqrt(T))
    disc = math.exp(-r * T)
    if kind == "CE":
        th = first - r * K * disc * norm_cdf(d2)
    else:
        th = first + r * K * disc * norm_cdf(-d2)
    return th / 365.0


def vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    T = max(T, MIN_T)
    d1, _ = _d1d2(S, K, T, r, max(sigma, 1e-4))
    return S * norm_pdf(d1) * math.sqrt(T)


def implied_vol(premium: float, S: float, K: float, T: float, r: float, kind: str) -> float | None:
    """Bisection on [0.5%, 400%]; None when the premium is below intrinsic or unpriceable."""
    if premium is None or premium <= 0 or S <= 0 or K <= 0:
        return None
    T = max(T, MIN_T)
    intrinsic = max(0.0, S - K * math.exp(-r * T)) if kind == "CE" else max(0.0, K * math.exp(-r * T) - S)
    if premium <= intrinsic + 1e-6:
        return None
    lo, hi = 0.005, 4.0
    if price(S, K, T, r, hi, kind) < premium:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if price(S, K, T, r, mid, kind) > premium:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)
