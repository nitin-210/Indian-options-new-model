# India Options: NIFTY, SENSEX and NIFTY BANK signals

A local trading screen for **buying** NIFTY and SENSEX weekly options (and NIFTY BANK monthly
options) on an opening-range breakout. It tells you what to buy, where the stop and target
are (on the index and as an estimated premium), how many lots fit your max
loss after every charge, and whether the signal passes your risk rules.

**Signals only. Not financial advice. Nothing is ever placed.** There is no
order code in this app; you place every order yourself in the Angel One app.
Most people who trade options intraday lose money.

## Start it (Windows PowerShell)

One-time setup, from your `day-trading` folder:

```powershell
cd C:\Users\hemar\Documents\day-trading\india-options
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Every day (one line):

```powershell
cd C:\Users\hemar\Documents\day-trading\india-options; .\.venv\Scripts\python.exe -m app
```

Your browser opens the dashboard at `http://127.0.0.1:8765`. The older screen (settings,
journal, odds, option chain) is at `http://127.0.0.1:8765/classic`. Without keys in
`config\secrets.toml` it runs the **demo**: a synthetic day you can watch at 30x to 600x speed
(Pause, speed and Restart in the top bar).

| command | what it does |
| --- | --- |
| `python -m app` | live if your keys are set, otherwise the demo |
| `python -m app --demo` | the synthetic demo day |
| `python -m app check-login` | tests login, instrument file, quotes, history and the live feed |
| `python -m app --replay data\sessions\2026-11-09` | replays a recorded live day |
| `python -m app rebuild 2026-11-05` | saves a past day's 1-minute candles so you can replay it |
| `python -m app backtest` | tests the rule on Angel's past 5-minute history (about 4 years) and prints a report |
| `python -m pytest -q` | runs the tests |

(Use `.\.venv\Scripts\python.exe` in place of `python`.)

## The dashboard (v14)

- **Centre: live chart.** NIFTY 50, SENSEX, NIFTY BANK, or any stock you click; 1m, 5m or 15m
  candles. The opening range is shaded. Before a signal the breakout triggers are drawn; after it, the
  signal arrow, entry, stop and T1-T3, with the risk (rose) and reward zones.
- **Left: index calls.** BUY CE or BUY PE, the contract, entry, stop, T1-T3 on the index and the
  premium now. Before a signal: "CE above 25,622 (25 pts away)". After a wide-range day: "No trade today".
- **Below the chart: the plan for what you selected.** A ladder with the index level, the suggested
  strike's estimated premium and the next strike out's at T3, T2, T1, entry and stop; the price to buy at
  or below and the price to skip above; what 1 lot costs and loses at the stop; and why the call was made.
  T1-T3 are 1R, 2R and 3R reference levels: in testing, holding to 15:00 (or the stop) beat every fixed target.
- **Stars: how big the day's move is likely to be** (the move-size model, re-checked at 09:30, 09:45,
  10:15, 11:00, 11:45, 12:30, 13:15 and 14:00 IST).
  - 3 stars: a big move in the trade's direction is 50% likely or more.
  - 1 star: a small, range-bound day is 70% likely or more.
  - 2 stars: anything in between.
  - Its skill grows through the day (AUC 0.68 at 09:30, 0.76 at 11:00, 0.87 at 14:00; 0.5 is a coin
    flip). In the walk-forward test, a 3-star call at 11:00 was right about 59% of the time (69% at
    12:30), and 1-star days really were small 71-78% of the time. Stars inform; they don't change the rule.
- **Right: Today's picks and Top movers.** Picks are the 10 F&O stocks with the biggest first 15
  minutes and gap compared with their own normal daily range (the least-traded quarter is dropped; before
  09:30 they are ranked by gap). 3 stars = top 10% today: in testing those made a big move on 50% of days,
  against 29% for all stocks; 2 stars = top 25% (about 37%). Each pick shows a reference entry, stop,
  T1 and option. **These stock levels are not a tested signal**: the plain breakout lost money on stocks
  in testing (about -0.01R a trade, 2017-2026). The tested stock model is not in the app yet.
- **Stop rule banner.** The backtested model puts the stop on the far side of the opening range; the app
  still defaults to the middle. The banner switches it with one click (or keep the middle). The far side
  doubles the stop distance, so 1 lot may lose more than your max loss per trade; raise it in Settings.

## What's new up to v14.27

- **Trades to take** (top left): the only real trades - the two tested rules on NIFTY and SENSEX: the
  opening-range breakout (yesterday's direction, no trade after a wide-range day, stop on the far side of
  the first 15 minutes, trailing exit after +1R, out by 15:00) and the Supertrend(10,3) flip on 15-minute
  candles (stop at the line, out by 15:00).
- **Breakouts now** (left) and **Today's picks** (right): stock breakouts that are still running, each
  with its option, buy price, stop, 2R target and 1-lot risk; at most 5 and 3 at a time; a row leaves the
  list when its trade ends. Options too thin to trade are left out. Untested on stocks: paper trade them.
- **Paper trading** (one click, nothing is ever sent to a broker):
  - The bar under every chart: Buy CE, Buy PE, or Buy CE + PE (a straddle, same at-the-money strike);
    a - / + lots selector (1-50 lots); the contract, price, cost, stop/target and max loss are filled in.
  - Exits by itself: the tested rule's stop/trailing stop/15:00; a listed stock's stop (far side of the
    first 15 minutes), 2R target or 15:00; your own call: stop, 2R or the 15:15 square-off; a straddle
    leg: 15:15. Exit any trade yourself; a straddle has separate Exit CE / Exit PE buttons.
  - Auto straddle at 09:15 on NIFTY 50, NIFTY BANK and SENSEX (switch in the Paper trading panel).
  - "My paper trades" (pinned top left) and the Paper trading panel (top bar): account, open and past
    trades, and each trade's P&L split into price move (delta), time decay (theta), volatility, spread
    and charges. Reset with a chosen capital (1, 2, 3 or 5 lakh).
- **Replay speeds** 1x to 600x; a red bar warns when a newer version is installed than the one running.

Research behind these choices: [docs/RESEARCH.md](docs/RESEARCH.md).

## Using Upstox instead of Angel One

The app can take all its market data from **Upstox** instead (live prices, option chains, movers,
history for the odds and the backtest). It uses Upstox's read-only **Analytics Token**: one token,
valid for a year, no daily login, and it cannot place orders.

1. Go to upstox.com/developer/apps, open the **Analytics** tab and click **Generate Token**.
2. Copy `config/secrets.example.toml` to `config/secrets.toml` (if you have not already) and paste the
   token into the `[upstox]` section: `access_token = "..."`. Leave the `[angel]` section as it is.
3. Check it: `.venv/bin/python -m app check-login` (Windows: `.\.venv\Scripts\python.exe -m app check-login`).
4. Start the app as usual. Whenever the `[upstox]` token is set, the app uses Upstox.

Notes: Upstox has 1- and 5-minute history from January 2022 (Angel's goes back further), so the
backtest and the odds cover a shorter period. Set your own broker's charges in Settings (brokerage
per order etc.); the defaults are Angel One's. The live feed needs the `upstox-python-sdk` package
(in requirements.txt; the launcher installs it).

## Your keys

Copy `config\secrets.example.toml` to `config\secrets.toml` and fill in:
`api_key`, `client_code`, `pin` (your 4-digit MPIN) and `totp_secret` (the
text key under the QR code on smartapi.angelone.in → Enable TOTP).
`secrets.toml` is git-ignored, never logged, never printed and never sent to
the browser. Do not paste these values into a chat.

## The rule

1. Opening range = index high and low from 09:15 to 09:30 IST.
2. Signal = the first **completed** 5-minute bar that closes beyond the range by at least 0.2 x the
   range width (BUY CE above, BUY PE below), **in the direction of yesterday's move**: if the index
   closed above its open yesterday, only CE signals count today; if it closed below, only PE. A
   breakout the other way is skipped (shown on the card) and the app keeps watching. Example: range 25,580-25,650 (70 points) needs a close
   above 25,664 or below 25,566. A wick is not a signal. No signals from bars closing after 14:00 IST.
   One signal per index per day. (Settings: breakout_buffer; 0 = any close beyond the range.)
3. Contract: nearest weekly expiry from Angel's instrument file (NIFTY Tuesday, SENSEX Thursday; a
   holiday moves it earlier). On expiry day the next expiry is used and flagged. Strike ATM (or 1 ITM).
4. Stop on the index: range midpoint (or the far side). **No fixed target**: exit at the stop or at
   15:00 IST, whichever comes first (version A). 1R, 1.5R and 2R are drawn as reference levels only.
   Version B: sell half at +1R and move the rest's stop to entry (needs 2+ lots). The paper journal
   records both A and B for every trade so you can compare them.
   (Settings: exit_mode = "target" takes profit at target_r instead.) Each level is converted to an
   estimated premium with Black-Scholes at the option's own IV.
5. Lots = the most lots whose loss at the stop, after brokerage, STT, exchange charges, GST, stamp
   duty, SEBI fee and slippage, fits your max loss per trade.
6. Wide-day skip (v14, part of the tested model): if yesterday's range was more than 1.2x its normal
   (14-day average) range, the index sits the day out. Settings: skip_after_wide_day, wide_day_atr.
7. Risk check, PASS or BLOCK with reasons: max loss, daily loss (3x by default), trades a day, time
   window, expiry day, bid/ask spread, premium too small or large, signal older than 10 minutes,
   price already past stop or target, data not live, and your limits not yet set.

Yesterday's-direction filter (added in v9): on 2025-2026 data that was not used to choose it, it
raised the average from about +0.08R to +0.22R a trade on NIFTY and from +0.11R to +0.25R on SENSEX,
with about half as many trades. Win rate is about 45% (version A) or 55% (version B).

Why these two rules (tested in Nov 2026 on Angel One history, Aug 2022 to Nov 2026): the original
rule (any close beyond the range, 2R target) lost slightly after charges. Requiring a stronger close
and letting winners run to 15:00 turned it positive on both NIFTY and SENSEX, in every year, and on
2025-2026 data that was not used to choose them (about +0.08R a trade on NIFTY and +0.11R on SENSEX,
estimates). Almost all of that profit comes from a handful of big trend days, so it only works if
every signal is taken and held. Capping winners at 3R removes the edge. Run `python -m app backtest`
to see the full comparison.

NIFTY BANK (v14) uses the same rule on monthly options (last Tuesday of the month from September
2025), but it was **not** part of the backtest, so its card is tagged "Untested". Its move-size rating
uses the NIFTY model scaled to its level (approximate).

UK times: in winter the market runs 03:45 to 10:00 UK, the opening range ends 04:00 UK, the
first signal can come at 04:05 UK, the last at 08:30 UK and the time exit is 09:30 UK. From
28 March 2027 (British Summer Time) add an hour; the app does this by itself.

## The classic screen (/classic)

- **Status strip**: market open or closed, IST and UK clocks, feed state and latency, India VIX, time
  to expiry, today's paper P&L against your daily limit, trades used.
- **Signal card**: the trade in one sentence. Click any field for "what this means".
  - **Entry**: the premium to take the trade at: a limit price (the ask when the signal fired plus a tick)
    and a "skip above" price. Past it, the rest of the move to the target pays less than 1:1 (Settings:
    "Don't chase below gain:loss"). A live green/red line says which side of it the premium is now.
  - **Compare strikes**: 2 ITM to 2 OTM with premium, delta, spread, lots, loss at the stop and gain at the
    target after charges and time decay. The best fit is marked: delta 0.40–0.70, spread within your limit,
    premium in range, 1+ lot inside your max loss, then the best gain-to-loss (near-ties go to delta ≈ 0.55).
    Settings → Strike → "Best fit" makes the plan use it; the default stays ATM.
- **Top movers**: the day's top bullish and bearish F&O stocks, ranked live by % change from yesterday's
  close, each tagged with its futures build-up (Long build-up, Short covering, Short build-up, Long
  unwinding). Click one for its at-the-money call and put: premium, bid/ask, IV, delta, cost of one lot.
  It is a list of movers, not signals; the breakout rule covers NIFTY and SENSEX only.
- **Chart**: 1m / 5m candles, the opening range shaded, the signal arrow, entry, stop and targets.
- **Which strike to buy (stocks)**: the stock panel compares 2 ITM to 2 OTM for the mover's direction (gainer:
  CE, loser: PE) using the index rule (stop at the middle of the stock's 09:15-09:30 range, 2R as the
  reference target): loss at the stop and gain at 2R after charges and time decay, and picks the best fit
  (delta 0.40-0.70, spread within your limit, 1 lot inside your max loss). Not backtested on stocks.
- **Stock charts**: click any stock in Top movers to open its 1m / 5m candles in the main chart
  (with its opening range and previous close). After the close it shows that day's session. Click
  NIFTY, SENSEX or the x to go back. Stock charts are for reference; signals cover NIFTY and SENSEX only.
- **Option chain**: ATM ±10 strikes, OI bars, change in OI, IV, LTP. Hover for delta, theta and
  bid/ask; click for that option's live premium chart.
- **Odds**: the same rule on past 5-minute history, and a 10,000-run Monte Carlo replay (seed 19).
- **Journal**: every signal is logged as a paper trade automatically (bought at the ask, sold at the
  bid), with win rate and P&L over time. Blocked signals are tracked separately.
- **Alerts**: press "Turn on alerts" once per session for sound, browser notifications and a
  screen wake lock. Keep the PC awake (Settings → System → Power → Screen and sleep: Never while
  plugged in) if you want the 04:05 UK alert.

If the feed drops, a red banner says so, prices grey out, nothing is shown as live and every
signal is blocked until data returns. The feed reconnects by itself.

## What Angel One's SmartAPI can and can't give

- Live ticks over WebSocket V2 (LTP, OHLC, volume, OI, best 5 bid/ask), up to 1,000 subscriptions
  and 3 connections per account. This app uses about 90 for the indices and their option chains, plus about
  two per F&O stock (cash price and near-month future) for the movers panel, well under the limit.
- Angel's gainers/losers endpoint is not used: users report errors and wrong values from it, so the movers
  ranking is computed here from the live feed.
- Historical candles: up to 100 days of 5-minute bars per request, 3 requests a second; the app
  keeps fetching older data until there is no more and caches it in `data\history`. If Angel
  refuses a request part-way (HTTP 403), what was already downloaded is kept and the next run
  carries on from there; a refusal at the same date twice is taken as the start of Angel's history.
- **No prices for expired options.** So the backtest's option P&L is a Black-Scholes estimate using
  India VIX. Live paper trades use real premiums.
- Greeks come from a REST endpoint only (not the feed), so IV, delta and theta are worked out here.
- A static IP is only required for placing, changing or cancelling orders, which this app never does.
- No holiday API: holidays are in `config\holidays.toml` (add 2027 when NSE publishes it).

Why not the official SDK? smartapi-python 1.5.5 writes request headers and bodies (including your
API key and PIN) to `logs\<date>\app.log` when a request fails, and turns TLS certificate checks
off for the live feed. This app talks to the same endpoints with its own small client instead.

## Files

```
app\        server, Angel client, feed, candles, Black-Scholes, strategy, costs, risk, odds,
            journal, engine, demo/replay sources
web\        dashboard.html/.js/.css (the dashboard), index.html (classic screen), TradingView Lightweight
            Charts (Apache-2.0), Chakra Petch and Manrope fonts (SIL Open Font License), all served locally
app\models  daytype.json: the move-size model (LightGBM trees, evaluated in plain Python)
config\     settings.example.toml (your settings.toml is created by the Settings tab), holidays.toml,
            secrets.example.toml (copy to secrets.toml)
data\       created on first run: logs, caches, history, recorded sessions, journal.db
tests\      unit tests (run offline)
```

Security: the server listens on 127.0.0.1 only, refuses requests addressed to any other host name,
and only accepts changes from its own page.
