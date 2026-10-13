# Backlog

**Convention.** Work through the open items from the top. Each has a "done when" so it's clear
when it's finished. Changes go through the gates (CLAUDE.md). Stop only at the stop-and-ask list
(the hard rules, a gate that can't resolve): write the item under "What needs you" in STATUS.md,
send `tradebot notify`, and move on to the next item you can do. Tick an item off with the commit
that did it. Go-live items are the owner's; they are listed so they aren't forgotten.

Paper numbers are not comparable with the backtest until the bot runs on the always-on box: the
Mac gave 24% uptime over 09-27 to 10-03 and 16% over the first weekly window.

## Open

- [ ] **The take-profit rule in the backtest** (fidelity, found reading the code 2026-10-13; no paper
  trade has reached its target yet). Paper takes profit as soon as the polled price touches the target
  (`bot._check_position`, every 30 s); the backtest only when a candle of the strategy's timeframe closes
  at or above it (`engine.simulate_trade`), so a 4h or daily wick through the target is an exit in paper
  and not in the backtest. On the exchange the take-profit trigger is the touch rule too.
  - Done when: the backtest models the touch rule (with the same-candle order of stop and target stated),
    a test shows a wick through the target exits, the frozen reference rerun and ledger notes follow, and
    the same-trades check stays identical (live is unchanged).
- [ ] **The `ml` protocol** (CLAUDE.md template, family `ml`): decides whether the ML filter comes
  back, on untouched data - at least 100 satellite trades after the reference's data end
  (2026-09-27), so not before the paper reserve has them. The replay models the filter
  walk-forward since b068c25 (`portfolio-backtest --ml-replay`); on the reference file it is
  information only (ledger note `ml-replay-a2`: satellite Sharpe 0.58 without, 0.38 with when the
  learns train as live's do on every configured timeframe, 0.64 with on the replayed timeframes
  only: the sign flips with the training set; the strategies were selected on that data).
  - Done when: registered, run once, recorded; only a pass sets `ML_IN_REPLAY = True`, through gate 1.
- [ ] **Sleeve transfers and the breakers' thresholds** (low, from the 2026-10-13 review): live shifts
  `peak_equity` and `day_start_equity` by the amount moved between sleeves (`_shift_breakers`), so the
  distance to both thresholds changes with each transfer; the replay's normalised satellite path
  doesn't see transfers. A few tenths of a percent around the first split and each 30-day reset.
  - Done when: either the replay carries the sleeve amounts through resets (model live as it is), or
    the owner decides live should preserve the ratio instead (a live change: stop-and-ask).
- [ ] **Live vs backtest candle history** - live computes indicators on the last 1000 candles at
  most, the backtest on the full history. Signals reported as "scanned, live found no signal" (and
  paper-only "entry condition false on the full history") measure the difference.
  - Counts only weeks with uptime above 95%.
  - Done when: for each such signal over four such weeks, the report compares the entry condition
    on candles cut like the scanner's with the backtest's, and says whether truncation explains it.
    A fix on the backtest side is a fidelity fix; anything on the live side is a stop-and-ask.
- [ ] **Weekly automation on the box** - the cron is installed (2026-10-12: Monday weekly, daily
  drawdown gate, nightly backup). The weekly's push needs the owner's `gh auth login` for the
  tradebot user (Needs the owner); the first cron weekly is 2026-10-19.
  - Done when: two Monday entries in STATUS.md came from cron, each with uptime above 95% and a
    Telegram summary sent, and the push went through.

## Go-live (stop and ask: the owner decides each)

- [ ] **Every protective exit on the exchange.** Built for the testnet path (2026-10-13, see STATUS.md):
  at entry an OCO pair on the exchange - a take-profit trigger and the stop-loss; breakeven and trailing
  move the exchange stop in one cancel-replace request (no gap between requests; a move through the price
  sells at once, a refused one puts a stop back at once, a lost answer is resolved by client id, a crash
  mid-move is settled at start-up). The take-profit leaves the exchange at the first move: Binance spot
  can't keep it across a move without about 0.4 s with no stop (your call, under Needs the owner).
  - Done when: `tradebot testnet verify` passes on the testnet (waits for the testnet keys), and the
    owner accepts the take-profit trade-off (the readiness check reports it until then).
- [ ] **Core sleeve live order path.** Built for the testnet path (2026-10-13): market orders with a
  client id saved before sending, the ledger from the fills, a lost answer resolved by client id; the
  core's cash and coins kept out of the satellite's equity, free cash and coin checks. `mode: live` is
  refused in code (`config.LIVE_TRADING`).
  - Done when: `tradebot testnet verify` passes its core step on the testnet.
- [ ] **The ML filter**: off in code since 2026-10-10. It comes back only through a2 and a passed
  `ml` protocol; until then paper and the backtest take the same signals at the same size.
- [ ] **The core/satellite split before live.** The restated finding (STATUS.md, 2026-10-07): no
  measurable effect of the satellite either way. The case for a larger core is simplicity and the
  same-regime structure, not a measured cost.
- [ ] **Testnet run** with the final settings, before any real money: `mode: testnet` exists (its own
  config and state, the paper bot's strategies); running an instance for a few weeks is the owner's call.
- [ ] **Paper track record** - the readiness checks in `tradebot report` pass.

## Needs the owner

- [ ] **Testnet keys** (testnet money only; DEPLOY.md step 10): `TESTNET_API_KEY` and `TESTNET_API_SECRET`
  from testnet.binance.vision in `~/tradebot-agent/.env`, so `tradebot testnet verify` can run.
- [ ] **The take-profit across stop moves** (a trade-off only the owner can make): keep the stop gap-free
  and the take-profit off the exchange after the first move (built), or keep the take-profit on the
  exchange and accept about 0.4 s with no stop on every move (not built).
- [ ] **The drawdown halt's resume** (an assumption to confirm): live halts new entries at 15% below
  the satellite's peak until the owner sends /resume (which restarts the peak). The replay assumes
  the resume at the next daily close. Say if a halt would stay longer in practice; the replay
  would model that instead.
- [ ] **Move the bot to an always-on Linux box** (DEPLOY.md) - done except the key above.

## Later

- [ ] **Rank by validated edge?** (low priority; needs untouched data and a registered protocol).
  With every signal counted, taken (+0.074R) and skipped for lack of room (+0.078R) are equal on
  average, and momentum@1d's taken equal its skipped; only breakout@1d (23 taken) shows a gap. Much
  of what motivated the idea came from the old candidate construction.
- [ ] **Point-in-time universe** - the backtests use today's most traded coins, which flatters the
  satellite (survivorship). Done when: a universe built from volume ranks as of each date
  (including delisted coins) exists, and any use of it in a verdict goes through a registered
  protocol.

## Done

- [x] Push credentials for the box: `gh` logged in as tradebot (owner, 2026-10-13); pushes work
- [x] The same-trades dataset extended (owner's item): three stored scenarios run through the real bot
  after the plain replay - a feed that publishes candles late, fetches that fail and recover, a stored
  logistic model that filters and sizes signals - each recorded in its own golden section
  (`gate same-trades --record-scenarios`, first time only; a changed scenario is a failure-path change,
  re-recorded the same way after review; the plain orders stay byte-identical and still decide whether a
  registered test is needed) (2026-10-13)
- [x] Sizing from the marked equity: the replay sizes and caps from the satellite's equity at the signal
  with open positions at the last close, as live does (01a7fcd); reference inside rounding
- [x] Reconcile: the coin list and strategies in force at the logged scan, not at the close plus a lag
  (01a7fcd): the first weekly window's six paper-only signals are shared rows, 47 of 51 agree, paper-only 0
- [x] The daily loss limit and the max-drawdown halt in the account replay, on its own equity path
  valued at each signal; window replays seeded at paper's sizes and outcomes; the reconcile names a
  breaker disagreement with both sides' numbers (2362e5f, b068c25; CLAUDE.md rule amended by the
  owner 2026-10-13). Reference: combined Sharpe 1.10 -> 1.10, satellite 0.59 -> 0.58.
- [x] Candles pinned behind frozen reruns: universe files record each coin-timeframe's first and
  last candle, rows and hash; a rerun says identical or lists what differs; the reference re-stamped
  with the box's pins and the weekly switched to it (2362e5f, b068c25, 2026-10-13)
- [x] a2. The ML filter in the account replay, walk-forward (b068c25); the `ml` protocol stays open
- [x] a. The ML filter: what it did - the 10-04 01:25 learn promoted a model (threshold 40%); on
  until the 10-12 deploy; 89 scored, 65 filtered, 24 sized up over 10-04 to 10-12 (STATUS.md
  2026-10-12)
- [x] b. The 6 paper-only signals: coins that joined the list at the scan; the 10-04 learn kept the
  selection (STATUS.md 2026-10-12). The reconcile refinement is the "coin list at the scan" item.
- [x] c. Core targets 10 of 12: the Mac's run lacked the 10-04 candle; rerun 12 of 12; the fetch-first
  core fix (owner, 2026-10-10) deployed 10-12 (STATUS.md 2026-10-12)
- [x] On the box: deployed 391661c through gate 1 (10-12 22:18 UTC), drawdown limits set from the
  reference backtest watching paper from that deploy, the agent's Telegram, the cron (2026-10-12)
- [x] BACKLOG a, b, c answered from the bot's data (STATUS.md 2026-10-12)
- [x] Ledger notes: replay notes on sizing-open-risk-budget-v1 and core-robustness, the process
  note and reading on core-btc-pre2017-v1 (2026-10-12)
- [x] The gates that replace approvals: same-trades check (fixed dataset, golden orders), deploy
  gate, research protocols (register/run/record once, trial counts), drawdown gate, `notify`,
  weekly Telegram summary, cron scripts; ML filter off in code; core fetch-first with a 6-hour
  retry (71f9573)
- [x] One settings hash in every report (the config file as loaded; each run's timeframes and core
  fraction printed beside it) (2026-10-07)
- [x] The agent writes only to its own state and data; the bot's database is read-only from the
  agent's checkout (`observe_state_dir`); `run` and `learn` refuse there (2026-10-07)
- [x] Confirm the replay-fix rerun used the 09-27 selection - recovered from the activity log;
  stamped `universe-20260927-1513@ae152bd-178b23c9a0.json` (now the weekly reference), no
  setting changed (ae152bd, run 10-05)
- [x] Attribute the 385 bps exit - PUMP momentum@4h trailing stop, 09-27 16:00, sold on waking
  after the Mac slept 15:28-16:53; +0.27R became -0.12R (ae152bd reconcile, 09-27 to 10-03)
- [x] Explain the 09-30 daily close - bot not running at the close (ae152bd reconcile)
- [x] Universe files save the full strategy selection; frozen reruns use it (feb0d5c)
- [x] A failed learn is retried an hour later (feb0d5c)
- [x] Go-live check: every protective exit rests on the exchange (feb0d5c)
- [x] `tradebot weekly`, CLAUDE.md, BACKLOG.md, STATUS.md, DEPLOY.md (feb0d5c)
- [x] The account replay follows its own positions; slots are freed when a position is flat (4e812f4)
- [x] Uptime with causes, scan log, strict running test (4e812f4)
- [x] Paper vs backtest reconciliation (5cd9138)
