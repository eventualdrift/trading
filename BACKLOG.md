# Backlog

**Convention.** Work through the open items from the top. Each has a "done when" so it's clear
when it's finished. Stop at the stop-and-ask list in CLAUDE.md: write the item under "What needs
you" in STATUS.md and move on to the next item you can do. Tick an item off with the commit that
did it. Go-live and owner items are listed so they aren't forgotten; they are not yours to do
unless the owner says so.

Paper numbers are not comparable with the backtest until the bot runs on the always-on box: the
Mac gave 24% uptime over 09-27 to 10-03 and 16% over the first weekly window.

## Open

- [ ] **a. The ML filter: what it did** - the reconciliation over 09-27 to 10-03 found it on (the
  old "inactive" note here was wrong). It is on whenever a promoted model file exists, and a learn
  that doesn't promote a new model keeps the old one.
  - Built (this round): `tradebot report` names the model in force and what the last learn's
    training decided; the reconcile lists the selections in force with the ML flag for the window,
    the signals the filter blocked and what the backtest says they were worth, and the trades it
    sized up; the bot now logs each model's threshold and training end.
  - Done when: STATUS.md records, from a reconcile with this code over 09-27 to 10-03 and the
    weekly window, whether the 10-03 learn deployed a model (to "What changed"), how many signals
    it blocked, and their outcomes. The filter itself is not changed: the decision is the owner's.
- [ ] **a2. The ML filter in the account replay** (only if the owner keeps the filter). The
  replay takes every signal at 1x; paper filters some and sizes others up to 1.5x.
  - Done when: the replay scores each candidate with the model a weekly learn would have had then
    (trained only on trades closed before that learn, promoted only if it passed the gate, the old
    one kept otherwise), filters below the threshold and applies the size multiplier; a test
    proves no model sees a label from after its learn; a frozen rerun on the reference file and a
    ledger note follow. Judging the filter is a separate registered test on untouched data.
- [ ] **b. The 6 paper-only signals after 10-03 12:00** (paper 51, both 45 in the first weekly).
  - Built (this round): each paper-only row now says why the backtest had none (no candles; candle
    after the data end or missing; history shorter than the warm-up; strategy not in the selection
    for that time; entry condition false on the full history; not replayed); the reconcile lists
    the selections in force; the weekly lists learn's changes from the activity log.
  - Done when: STATUS.md names the reason for each of the six, and whether the 10-03 learn changed
    the selection.
- [ ] **c. Core targets matched 10 of 12 coin-days in the weekly** (8 of 8 before 10-03).
  - Built (this round): each mismatch is explained: no daily candles at the bot's check (the fetch
    failed, e.g. right after waking), or the candle and close the bot used against the backtest's
    (the bot logs them from now on).
  - Done when: STATUS.md names the two coin-days and the cause. If it is a failed fetch: the bot
    marks the day done before fetching, so that day's rebalance is skipped. A retry would change
    the core's trades: stop and ask.
- [ ] **Live vs backtest candle history** - live computes indicators on the last 1000 candles at
  most, the backtest on the full history. Signals reported as "scanned, live found no signal" (and
  paper-only "entry condition false on the full history") measure the difference.
  - Counts only weeks with uptime above 95%.
  - Done when: for each such signal over four such weeks, the report compares the entry condition
    on candles cut like the scanner's with the backtest's, and says whether truncation explains it.
    A fix on the backtest side is a fidelity fix; anything on the live side is a stop-and-ask.
- [ ] **Weekly automation on the box** - after the move (DEPLOY.md).
  - Done when: cron runs `tradebot weekly` every Monday and pushes STATUS.md, and two weekly
    entries exist with uptime above 95%.

## Go-live (stop and ask: the owner decides each)

- [ ] **Every protective exit on the exchange.** Breakeven, trailing and take-profit stops are
  bot-managed, so they don't fire while the bot is down.
  - The real case: PUMP momentum@4h's trailing stop was hit at 09-27 16:00 while the Mac was asleep
    (15:28-16:53). Paper sold on waking, 385 bps lower, and +0.27R became -0.12R.
  - Done when: live keeps each one as an exchange order, moved without a window in which the
    position is unprotected, verified on the testnet; the `tradebot report` check "every
    protective exit rests on the exchange" passes.
- [ ] **The ML filter decided**: off, or on and modelled in the replay (a2) and judged by a
  registered test. Today paper and the backtest differ by design while it is on.
- [ ] **Core sleeve live order path** - the core is paper-only today.
  - Done when: built, reviewed, testnet-verified, and live mode no longer refuses it.
- [ ] **The core/satellite split before live.** The restated finding (STATUS.md, 2026-10-07): no
  measurable effect of the satellite either way. The case for a larger core is simplicity and the
  same-regime structure, not a measured cost.
- [ ] **Testnet run** with the final settings, before any real money.
- [ ] **Paper track record** - the readiness checks in `tradebot report` pass.

## Needs the owner

- [ ] **Move the bot to an always-on Linux box** (DEPLOY.md) - in progress.
- [ ] **Ledger notes for core-btc-pre2017-v1** (the process note and the reading), if they
  haven't been added yet.

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
