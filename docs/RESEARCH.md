# Research log: what was tested and what made it into the app

Every number below is for **1 lot** of at-the-money options, with spread, slippage and Indian charges, unless it
says otherwise. Option prices come from Black-Scholes with India VIX and the app's time-decay model (decay spread
over calendar time). The "stress" check counts all decay inside market hours; most option-buying strategies lose
under it, so it is a worst case, not the expectation. Data: NIFTY 5-minute 2015-2026, SENSEX 2018-2026,
NIFTY BANK Oct 2022-2026, 55 large-cap stocks with volume Dec 2022-Nov 2026, 1-minute NIFTY 2011-Feb 2023,
SENSEX Mar 2018-Mar 2024 and 70 stocks 2017-2021. Strategies were chosen on the older 60% of days and checked
on the newer 40%. Scripts are in `research/` (they point at the research machine's data folders).

## In the app (tested)

| Rule | Trades a month | Wins of 10 | Profit a trade | Profit a month | Months in profit |
|---|---|---|---|---|---|
| Opening-range breakout, yesterday's direction, skip after a wide day, far-side stop, trailing exit | 13.3 | 4.8 | +₹432 | +₹5,754 | 63% |
| Supertrend(10,3) flip on 15-minute candles, stop at the line, out by 15:00 | 11 | 4.8 | +₹375 | +₹4,105 | 64% |
| **Both together** (NIFTY + SENSEX) | 24 | 4.8 | about +₹407 | **+₹9,859** (last 3 years +₹17,897) | 71% |

Average win about ₹2,524 against an average loss of about ₹1,542. No losing year 2015-2026.

## Tested and NOT added

| Idea | Result |
|---|---|
| Previous day's high/low breakout | Best version +₹245 a trade, 48% of months in profit; mostly the same days and direction as the opening-range rule. |
| "Double confirmation" (gap beyond yesterday's range + same-way 15-minute breakout) | +₹220 a trade, 5.0 wins of 10; gap-down + breakdown +₹505, gap-up + breakout +₹104; the plain rule earns +₹432. |
| Yesterday's range as confirmation | Our trades still inside yesterday's range earned +₹648 a trade, those already beyond it +₹289; stop-outs 26% vs 25%. A filter would cut the better half. |
| Flip after a failed (unconfirmed) breakout | +₹365 a trade, but +₹603 in older years and +₹78 since 2022; loses on NIFTY BANK. |
| 1-minute entries/exits (early entry, retest, tight 1-minute stops, 1-minute trailing) | None beat the 5-minute rules; tight stops lose (costs are 30-70% of a 1-minute stop's risk; half of all winners first dip below a tight stop). Pure 1-minute setups ≈ ₹0 or losing. |
| Support/resistance bounces, break-and-retest | ≈ ₹0 or losing; quick bounces win ~5 of 10 but lose ₹114-130 a trade after costs. |
| Demand/supply zones that already held, trendlines | Zone result not robust (positive in 7 of 24 settings); trendlines lose. |
| Liquidity sweep + structure shift against the 15-minute trend | **+₹207 a trade, positive in 15 of 15 settings**, 3.6 wins of 10, adds ~₹2,500 a month; weaker since 2023. Candidate for paper tracking. |
| Stop hunt then back with the trend; first-hour sweep of yesterday's high/low | ≈ ₹0 / inconsistent across years. |
| Stock breakouts (top movers, picks, any break) | About 0R before costs, losing after option costs (≈ -₹60 to -₹100 a trade). |
| Stock trend score (relative volume, strength vs NIFTY, NR7, squeeze, 20-day trend) | Trend vs sideways: no better than the picks score (AUC 0.58); direction a coin flip (49-51%). |
| 09:15 long straddle (NIFTY/SENSEX/NIFTY BANK), sold at 15:15 | +₹549 / +₹803 / +₹354 a day with the app's pricing but about -₹1,300 a day under the stress check: inconclusive; paper-trading it with live quotes. |
| Option payoff strategies from IJNRD2205074, hedges, pre-market stock selection, Fibonacci | Tested earlier; none improved the tested rules. |

## Still to test

- Futures open-interest build-up ("big money") on NIFTY/SENSEX and stocks: needs the app to record it first
  (Angel keeps only a few months of OI history).
- Real option prices: download Angel's option candles for live contracts to check the pricing model.
