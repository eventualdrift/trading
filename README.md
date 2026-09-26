# tradebot

A crypto trading bot that scans the market for opportunities and either **sends you signals**
(entry, stop-loss, take-profit and when to sell) on Telegram or **trades your exchange account
for you**. It re-validates its strategies and retrains its ML filter on its own every week.

```
🟢 BUY SIGNAL — SOL/USDT (4h)
Strategy: trend · Win probability: 64%

Entry:        142.350  (market, now)
Don't chase:  above 144.015
Stop-loss:    136.800  (-3.90%)
Take-profit:  153.450  (+7.80%)
Reward:risk:  1:2.0
Size:         1.8 SOL (~256.23 USDT), risking 9.99 USDT (1.0%)
Time limit:   close by 2026-10-05 16:00 UTC if neither level is hit

Why: uptrend (EMA50 > EMA200, ADX 27); pullback over, RSI turned up at 46
#12 · paper mode
```

…followed later by `🔒 Move stop to breakeven` and `✅ SELL NOW — take-profit hit, +2.00R`.

> **Read this first.** No bot can promise profits, and most "self-learning AI bot" videos show
> a cherry-picked winning streak. This bot is built to be *honest* rather than impressive:
> every strategy must prove itself on data it has never seen, the ML filter is only deployed if
> it beats the plain strategy out-of-sample, and live trading is refused until your paper
> track record passes a checklist. It may decide that nothing is worth trading, and then it
> won't trade. Only use money you can afford to lose.

---

## How it works

```
             ┌──────────────── weekly self-learning cycle ────────────────┐
             │                                                            │
 exchange ──►│ 1. download history (top-N pairs, 15m/1h/4h/1d)            │
 (ccxt)      │ 2. backtest every strategy × timeframe with fees+slippage   │
             │    → keep only combos profitable in-sample AND out-of-sample│
             │      and on ≥ half the coins                                │
             │ 3. label every historical setup win/loss, train a gradient- │
             │    boosted model walk-forward; deploy only if it beats the  │
             │    unfiltered strategy on unseen data                       │
             └────────────────────────────┬───────────────────────────────┘
                                          ▼
 every candle close:  scanner → strategy setups → ML win-probability → rank
                                          ▼
                      risk manager (1% risk sizing, limits, circuit breakers)
                                          ▼
              paper broker (simulated)  or  live broker (real orders + exchange stop)
                                          ▼
 every 30s:   watch open trades → take-profit / stop / breakeven / time exit
                                          ▼
                      Telegram: signals, exits, daily summary, /commands
```

**Strategies** (all long and short capable, entries on closed candles only):

| name       | idea                                                                   |
|------------|------------------------------------------------------------------------|
| `trend`    | buy the end of a pullback inside a confirmed trend (EMA50/200, ADX, RSI) |
| `breakout` | buy a close above the 20-bar range on 1.5× volume, with the EMA200 trend |
| `meanrev`  | in ranging markets (low ADX) buy an oversold dip back inside Bollinger |
| `momentum` | trend following: buy a new 55-bar high in a trending market, no near target - the stop trails 3 ATR behind the best price once +1R, so the occasional big trend pays for the small losers |

Every trade has an ATR-based stop-loss, a fixed reward:risk take-profit, a breakeven move at
+1R, a time limit and a strategy-specific early exit.

**The "self-learning" part** is real, but deliberately cautious:
- *Strategy selection* is re-run weekly on fresh data, so the bot stops trading what stopped working.
- *The ML filter* (scikit-learn gradient boosting over ~40 features) estimates the probability that a
  setup wins. It's trained walk-forward with an embargo (no future leakage), its threshold is
  picked on a calibration block, and it replaces the current model only if it's better on a
  held-out test block. It must also show that the trades it takes beat the ones it rejects by
  more than luck would (a Welch t-test, t ≥ 2.5). Without that check, about half of models
  trained on pure noise got through; with it, about 1 in 60 do. The bot's own closed trades are
  added to the training data.

**BTC market context.** Every strategy can optionally trade only while BTC is above its 200-day
average. With `selection.btc_filter: auto` the learning cycle tests each strategy both ways and
keeps the filter only where it improves results in-sample **and** out-of-sample. The learning
summary says which filters it kept, and how many variants it tried.

## Core + satellite (paper)

To grow a small account, most of it can sit in a simple, low-turnover trend allocation, with
the signal strategies running on the rest:

- **Core** (`core.fraction`, e.g. 65%): BTC and ETH, an equal slot each. At every daily close each
  coin's weight is the share of its 50/100/150/200-day averages that the price is above, so 0%,
  25%, 50%, 75% or 100% of its slot. The core trades only when a weight steps or a holding has
  drifted by 20% of its slot, and never for less than `core.min_trade_usd`. So it's in the market
  during uptrends and mostly in cash in bear markets.
- **Satellite** (the rest): the validated signal strategies, with risk per trade as a % of the
  *satellite's* equity. Its drawdown breakers look at the satellite only.
- Both sleeves compound with their own equity (position sizes grow as the account grows). Every
  30 days (`core.rebalance_sleeves_days`) the account is reset to the target split.
- Each sleeve's P&L is kept separately and shown in `/status`, `tradebot report` and the dashboard.

Check the idea on real history before switching it on:

```bash
tradebot portfolio-backtest --capital 1000 --core-fraction 0.65
```

This backtests the combined account, the core alone and the satellite alone against holding
BTC and holding BTC+ETH. It covers the full history and the period since 2022, and gives the
return, return per year, worst dip, Sharpe ratio and a year-by-year table, all after fees and
slippage.

The core sleeve is **paper-only** in this version: `mode: live` refuses `core.fraction > 0`.

## Quick start

Requires Python 3.10+.

```bash
git clone <this repo> && cd trading
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

tradebot demo          # offline end-to-end run on synthetic data (≈1–2 min)
pytest -q              # 192 tests
```

### 1. Configure

```bash
tradebot init          # creates config.yaml and .env from the examples
```

- `config.yaml`: exchange (`binance` by default, any [ccxt exchange](https://github.com/ccxt/ccxt#supported-cryptocurrency-exchanges) works), pairs, risk settings.
  Check that your exchange serves your country.
- `.env`: secrets. You don't need exchange API keys for signals/paper mode.

### 2. Telegram (signals on your phone)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. `tradebot telegram-test` prints your chat id → put it in `TELEGRAM_CHAT_ID`.
4. `tradebot telegram-test` again → you should receive a test message.

Only your chat id can control the bot. Messages from anyone else are ignored.

### 3. Learn, then paper trade

```bash
tradebot learn         # download history, select strategies, train ML (a few minutes)
tradebot scan          # see what it would signal right now
tradebot run           # 24/7: signals to Telegram + paper trading with fake money
```

Follow the signals by hand if you like. The paper account tracks exactly what following
every signal would have done.

### 4. What could it make? `tradebot project` and `tradebot portfolio-backtest`

```bash
tradebot project --capital 1000
```

This simulates 5,000 possible futures for your account. It samples the selected strategies'
out-of-sample trades, which were never used to choose them, at their historical rate, using
the live risk rules and fees. It prints a bad / typical / good range and the chance of being
down at 1, 2, 3, 6 and 12 months, plus the worst dip to expect. It also compares against
**simply holding Bitcoin** over the same period, because a bot that makes less than buying
Bitcoin and waiting isn't worth running. It's a range based on the past, not a promise.

`tradebot portfolio-backtest` (see [Core + satellite](#core--satellite-paper)) answers the
same question for the whole account with a core sleeve.

Optional rules are evaluated on real data before you switch them on:

```bash
tradebot research breaker     # would pausing entries during BTC volatility bursts have helped?
```

It prints each selected strategy with and without the breaker, in-sample and out-of-sample.
It recommends `guards.vol_breaker: true` only if the breaker helps every strategy in both periods.

### 5. Check the track record

```bash
tradebot report          # example output below
tradebot dashboard       # writes state/dashboard.html - see "Dashboard" below
```
```
Go-live readiness (paper track record):
  [PASS] enough trades: 41 closed paper trades (need 30)
  [PASS] long enough: 23.5 days of paper trading (need 14)
  [PASS] positive expectancy: +0.214R per trade
  [PASS] profit factor: 1.46 (need 1.2)
  [PASS] drawdown within limit: max drawdown 6.2% (limit 15.0%)
  => READY for live trading (start small!)
```

### 6. Go live (only when ready)

Live trading is supported on **Binance spot** only. Signals and paper trading work with any
ccxt exchange, but automatic trading needs exchange-specific handling of stop orders, order
lookups and fills that must be verified on a testnet first. OKX's trigger orders and Bybit's
account modes, for example, behave differently. Other exchanges are refused until they get
that treatment.

1. Use a **dedicated account or sub-account** for the bot, and don't hold or trade the same
   coins there by hand - the bot reconciles positions against the account balance.
2. Create an API key with **trade permission only**, **withdrawals disabled**, and
   **IP-whitelisted** to your server. Put it in `.env`.
3. Run on the exchange **testnet** first (`exchange.sandbox: true`) and watch a few full
   trades: entry, stop order visible on the exchange, exit.
4. Set `mode: live`, fund the account with a small amount and `tradebot run`.
   The bot refuses to start live if the paper checklist fails (`--force-live` overrides; don't).

How live orders are handled:
- Every order gets a client ID that's saved to the database *before* the order is sent. If
  the response is lost (e.g. a network timeout), the bot looks the order up by that ID once
  Binance's 10-second request window has passed. It never guesses from balance changes. If it
  still can't tell, it halts new entries, keeps checking, and `/resume` is refused until resolved.
- After each buy it places a stop-loss **on the exchange**, reads it back and checks it's a real,
  open stop at the right price. If that fails, the position is sold immediately
  (`live.require_exchange_stop`). So your downside stays protected even if the bot goes offline.
- To exit, it cancels the exchange stop, reads its final state and sells only what's still held,
  so a stop that fills at the same moment never causes a second sale. A sell whose outcome is
  unclear is resolved before any new sell. Partial fills are tracked per order and the rest retried.
- If a required stop can't be placed and verified, the position is sold and new entries halt.
- At start-up and hourly it cancels stray bot orders that no position owns.
- Take-profit, breakeven and time exits are market orders the bot sends at the current price.
  The exchange stop stays at the original level as a safety net.
- If the coins disappear from the account (e.g. sold by hand), the bot halts new entries and
  asks you to check. `/forget <id>` then removes a position from its books without trading.

## Dashboard

The bot writes `state/dashboard.html` every 5 minutes. Open it in a browser: it reloads itself
every minute. It shows:

- account equity against the same money held in BTC, and each sleeve's equity (core and
  satellite);
- equity, change over 7/30/90 days or all time, and each sleeve's P&L;
- open trades with live P&L in R;
- the core's target weights and holdings;
- recent signals with why each was taken, skipped or filtered;
- every alert the bot sent (fills, exits, halts, errors).

To view it live instead of opening a file, set `dashboard.serve: true` (served while
`tradebot run` runs) or run `tradebot dashboard --serve`. Then open http://127.0.0.1:8765. It
listens on this computer only. To see it from another device, use an SSH tunnel
(`ssh -L 8765:127.0.0.1:8765 you@server`); don't expose the port.
Under Docker the server isn't reachable from outside the container, so open the file
`./state/dashboard.html` instead.

## Side-by-side paper configs

To compare two setups over the same paper period (say, with and without the core sleeve), run
two instances. Each needs its own config file, `state_dir` and dashboard port. They can share
the price cache (`data.dir`), whose writes are atomic.

```yaml
# config-b.yaml - a copy of config.yaml with:
name: B                  # messages start with [B]; the dashboard title says B
state_dir: state-b       # its own database, strategy selection and ML model
core:
  fraction: 0            # the setting being compared
telegram:
  commands: false        # both can SEND to the same Telegram bot, but only one may answer /commands
dashboard:
  port: 8766
```

```bash
tradebot --config config-b.yaml learn
tradebot --config config-b.yaml run       # in a second terminal / service
tradebot --config config-b.yaml report
```

Telegram commands only reach the instance with `telegram.commands: true`. To control B from
Telegram too, give it its own bot token: create a second bot with @BotFather and point B at a
separate env file with `--env .env-b`.

## Telegram commands

| command | |
|---|---|
| `/status` | mode, equity, strategies in use, ML status |
| `/positions` | open trades with live P&L |
| `/signals` | recent signals (including skipped/filtered and why) |
| `/performance` | all-time and 7-day stats |
| `/pause` · `/resume` | stop / restart new entries (open trades keep being managed) |
| `/close <id>` | close one trade at market |
| `/closeall` | kill switch: close everything and pause |
| `/forget <id>` | remove a position from the books without trading (after fixing it on the exchange) |
| `/learn` | run the self-learning cycle now |

## Risk controls

| control | default |
|---|---|
| risk per trade (position sized so the stop-loss costs this much) | 1% of equity |
| max open positions / one position per coin | 3 / yes |
| max single position | 30% of equity |
| max total exposure | 100% of equity (no leverage) |
| minimum reward:risk | 1.5 |
| bigger bets on the strongest setups | up to 1.5x risk, only if the ML proved on unseen data that its most confident picks really earn more |
| trailing stop (momentum strategy) | once +1R, stop follows 3 ATR behind the best price; never loosened |
| daily loss limit → no new trades until 00:00 UTC | 3% |
| drawdown from peak → halt until `/resume` | 15% |
| don't chase: skip if price already moved past the signal | 0.3R |
| exchange-side stop-loss (live), verified after placing | on; sell if it can't be placed |
| bad-data guard: skip frozen feeds, confirm any >10% jump on the next poll, ignore stale prices | on |
| BTC volatility breaker: no new entries while BTC's recent volatility is 2.5× normal | off until `tradebot research breaker` supports it |

## Commands

| | |
|---|---|
| `tradebot init` | create `config.yaml` and `.env` |
| `tradebot learn` | self-learning cycle (data → selection → ML) |
| `tradebot backtest [--strategy trend] [--timeframe 4h] [--symbols BTC/USDT] [--trades-csv out.csv]` | portfolio backtest |
| `tradebot scan` | current opportunities, no trading |
| `tradebot run` | run the bot (paper or live per config) |
| `tradebot report` | track record + go-live checklist |
| `tradebot project [--capital 1000]` | range of outcomes for your account at 1-12 months, vs holding BTC |
| `tradebot portfolio-backtest [--capital 1000] [--core-fraction 0.65] [--since 2022-01-01]` | core + satellite account vs holding BTC |
| `tradebot research breaker [--ratios 2 2.5 3]` | evaluate the volatility breaker on real data |
| `tradebot dashboard [--serve] [--port 8765] [--out file.html]` | write or serve the dashboard |
| `tradebot telegram-test` | Telegram setup helper |
| `tradebot demo` | offline demo on synthetic data |

Add `--synthetic` to `learn`, `backtest`, `scan`, `project`, `research` or `portfolio-backtest` to try them
without internet access (results on synthetic data are meaningless). `--config` and `--env` pick
another config file and secrets file (for side-by-side instances).

## Running 24/7

A trading bot has to stay up, so run it on a small VPS (1 vCPU / 1 GB RAM is enough), not a laptop:

```bash
cp config.example.yaml config.yaml && cp .env.example .env   # edit both
docker compose up -d --build
docker compose logs -f
```

State (database, strategy selection, ML model) lives in `./state`; price history in `./data`.

## Project layout

```
tradebot/
  strategies/     trend, breakout, meanrev, momentum (vectorised, causal), optional BTC filter
  backtest/       engine (fees, slippage, conservative stop/TP), metrics, selection
  ml/             features, labelled dataset, walk-forward model with promotion gate
  data/           ccxt exchange client, candle cache, synthetic market
  execution/      paper broker, live broker (ccxt)
  notify/         Telegram + message formatting
  scanner.py      finds and ranks setups on each candle close
  risk.py         sizing and circuit breakers
  bot.py          main loop, position management, commands
  learning.py     the self-learning cycle
  report.py       go-live readiness, sleeve P&L
  context.py      BTC uptrend and volatility ratio, joined without look-ahead
  core.py         the core sleeve (trend weights, rebalancing) and its backtest
  portfolio.py    whole-account backtest vs holding BTC
  research.py     real-data studies of optional rules (volatility breaker)
  dashboard.py    HTML dashboard (file or 127.0.0.1 server)
tests/            192 tests: look-ahead checks, live-vs-backtest parity (signals and core),
                  a fake exchange with trigger-order routing, partial fills, races and timeouts
```

## Limitations and next steps

- Live trading is **spot, long-only**. Shorts only exist in paper/signal mode (`allow_short: true`).
- Live execution is tested against a simulated exchange that mimics each venue's order routing,
  not against the real exchanges. **Run on the testnet before real money.**
- Backtests are deliberately pessimistic: the stop wins when stop and target share a candle, a
  take-profit only counts when the candle closes beyond the target (wicks don't), and all exits
  pay slippage. They can't model order-book depth on small coins.
- Paper-only for now: the core sleeve and limit-order entries (`costs.entry_order: limit`).
  Live mode refuses both until their live order paths have been built and reviewed.
- Next: a strategy research phase with safeguards against luck. New strategies come from a
  written list, each with an economic reason, fixed in advance. Each must pass a final holdout
  used only once and be scored with a penalty for the number of variants tried (deflated
  Sharpe). Then it goes to paper trading, and gets capital only in proportion to its live
  track record.

*Not financial advice. You are responsible for any trades made with this software.*
