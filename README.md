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

Every trade has an ATR-based stop-loss, a fixed reward:risk take-profit, a breakeven move at
+1R, a time limit and a strategy-specific early exit.

**The "self-learning" part** is real, but deliberately cautious:
- *Strategy selection* is re-run weekly on fresh data, so the bot stops trading what stopped working.
- *The ML filter* (scikit-learn gradient boosting over ~40 features) estimates the probability that a
  setup wins. It's trained walk-forward with an embargo (no future leakage), its threshold is
  picked on a calibration block, and it replaces the current model only if it's better on a
  held-out test block. The bot's own closed trades are added to the training data.

## Quick start

Requires Python 3.10+.

```bash
git clone <this repo> && cd trading
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

tradebot demo          # offline end-to-end run on synthetic data (≈1–2 min)
pytest -q              # 117 tests
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

### 4. Check the track record

```bash
tradebot report          # example output below
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

### 5. Go live (only when ready)

Live trading is supported on **Binance, Bybit, Kraken and OKX** (the exchanges whose stop-loss
order handling has been checked against ccxt); other exchanges are refused.

1. Use a **dedicated account or sub-account** for the bot, and don't hold or trade the same
   coins there by hand - the bot reconciles positions against the account balance.
2. Create an API key with **trade permission only**, **withdrawals disabled**, and
   **IP-whitelisted** to your server. Put it in `.env`.
3. Run on the exchange **testnet** first (`exchange.sandbox: true`) and watch a few full
   trades: entry, stop order visible on the exchange, exit.
4. Set `mode: live`, fund the account with a small amount and `tradebot run`.
   The bot refuses to start live if the paper checklist fails (`--force-live` overrides; don't).

How live orders are handled:
- Every entry is written to the database *before* the order is sent. If the outcome is unclear
  (e.g. a network timeout), the bot checks the balance; if it still can't tell, it halts new
  entries and tells you.
- After each buy it places a stop-loss **on the exchange**, reads it back and checks it's a real,
  open stop at the right price. If that fails, the position is sold immediately
  (`live.require_exchange_stop`). So your downside stays protected even if the bot goes offline.
- To exit, it cancels the exchange stop, reads its final state and sells only what's still held,
  so a stop that fills at the same moment never causes a second sale. Partial fills are tracked
  and the rest is retried.
- Take-profit, breakeven and time exits are market orders the bot sends at the current price.
  The exchange stop stays at the original level as a safety net.
- If the coins disappear from the account (e.g. sold by hand), the bot halts new entries and
  asks you to check. `/forget <id>` then removes a position from its books without trading.

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
| daily loss limit → no new trades until 00:00 UTC | 3% |
| drawdown from peak → halt until `/resume` | 15% |
| don't chase: skip if price already moved past the signal | 0.3R |
| exchange-side stop-loss (live), verified after placing | on; sell if it can't be placed |

## Commands

| | |
|---|---|
| `tradebot init` | create `config.yaml` and `.env` |
| `tradebot learn` | self-learning cycle (data → selection → ML) |
| `tradebot backtest [--strategy trend] [--timeframe 4h] [--symbols BTC/USDT] [--trades-csv out.csv]` | portfolio backtest |
| `tradebot scan` | current opportunities, no trading |
| `tradebot run` | run the bot (paper or live per config) |
| `tradebot report` | track record + go-live checklist |
| `tradebot telegram-test` | Telegram setup helper |
| `tradebot demo` | offline demo on synthetic data |

Add `--synthetic` to `learn`, `backtest` or `scan` to try them without internet access.

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
  strategies/     trend, breakout, meanrev (vectorised, causal)
  backtest/       engine (fees, slippage, conservative stop/TP), metrics, selection
  ml/             features, labelled dataset, walk-forward model with promotion gate
  data/           ccxt exchange client, candle cache, synthetic market
  execution/      paper broker, live broker (ccxt)
  notify/         Telegram + message formatting
  scanner.py      finds and ranks setups on each candle close
  risk.py         sizing and circuit breakers
  bot.py          main loop, position management, commands
  learning.py     the self-learning cycle
  report.py       go-live readiness
tests/            117 tests: look-ahead checks, live-vs-backtest parity, a fake exchange with
                  trigger-order routing, partial fills, races and network timeouts
```

## Limitations and next steps

- Live trading is **spot, long-only**. Shorts only exist in paper/signal mode (`allow_short: true`).
- Live execution is tested against a simulated exchange that mimics each venue's order routing,
  not against the real exchanges. **Run on the testnet before real money.**
- Backtests: the stop wins when stop and target fall in the same candle, and take-profits pay
  slippage. They're still slightly optimistic when price pokes through the target and reverses
  within the bot's 30-second polling interval (live would miss that exit), and they can't model
  order-book depth on small coins.
- Possible additions: futures/shorting, multiple take-profit levels, a web dashboard,
  news/sentiment features, more strategy families.

*Not financial advice. You are responsible for any trades made with this software.*
