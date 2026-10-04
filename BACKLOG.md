# Backlog

**Convention.** Work through the open items from the top. Each has a "done when" so it's clear
when it's finished. Stop at the stop-and-ask list in CLAUDE.md: write the item under "What needs
you" in STATUS.md and move on to the next item you can do. Tick an item off with the commit that
did it. Go-live and owner items are listed so they aren't forgotten; they are not yours to do
unless the owner says so.

## Open

- [ ] **Confirm the replay-fix rerun used the 09-27 selection** - block 3 of the 10-03 round ran
  after Saturday's learn, so it may have mixed the replay fix with a selection change.
  - Done when: `tradebot portfolio-backtest --core-fraction 0.65 --universe-file
    reports/universe-20260927-1513@389af86-178b23c9a0.json` is rerun with the current code; its
    "Strategy selection used" line says "saved with the run" or "recovered from the bot's activity
    log" (not today's); the new stamped file (with the full selection) is set as the weekly
    reference (`tradebot weekly --reference <file>`); the like-for-like satellite finding
    (65/35 vs core at same exposure, from the satellite's first trade) is restated with the new
    numbers in STATUS.md. If the conclusion changes: stop and ask.
- [ ] **Attribute the 385 bps exit** - the second reconcile's one shared exit filled 385 bps
  worse than modelled (R -0.4).
  - Done when: STATUS.md names it (coin, setup, paper vs backtest reason and price), how much
    later paper sold, and why, from the reconcile's exit list on the same --since/--end.
- [ ] **Explain the 09-30 daily close** - six backtest signals (AAVE, ICP, PUMP, ...) at the
  2026-09-30 00:00 UTC close had no paper signal.
  - Done when: the reconcile over that window names the cause of each (bot not running and the
    gap's cause, or the candle skipped as stale), checked against the bot's log ("skipping stale
    1d candle", "1d scan:").
- [ ] **Live vs backtest candle history** - live computes indicators on the last 1000 candles at
  most, the backtest on the full history. Signals reported as "scanned, live found no signal"
  measure the difference.
  - Done when: for each such signal over four weeks, the report compares the entry condition on
    candles cut like the scanner's with the backtest's, and says whether truncation explains it.
    A fix on the backtest side is a fidelity fix; anything on the live side is a stop-and-ask.
- [ ] **Weekly automation on the box** - after the move (DEPLOY.md).
  - Done when: cron runs `tradebot weekly` every Monday and pushes STATUS.md, and two weekly
    entries exist with uptime above 95%.

## Go-live (stop and ask: the owner decides each)

- [ ] **Every protective exit on the exchange.** Breakeven, trailing and take-profit stops are
  bot-managed, so they don't fire while the bot is down.
  - Done when: live keeps each one as an exchange order, moved without a window in which the
    position is unprotected, verified on the testnet; the `tradebot report` check "every
    protective exit rests on the exchange" passes.
- [ ] **Core sleeve live order path** - the core is paper-only today.
  - Done when: built, reviewed, testnet-verified, and live mode no longer refuses it.
- [ ] **The core/satellite split before live** (leaning to a larger core). The satellite finding
  is provisional until the replay-fix rerun above.
- [ ] **Testnet run** with the final settings, before any real money.
- [ ] **Paper track record** - the readiness checks in `tradebot report` pass.

## Needs the owner

- [ ] **Move the bot to an always-on Linux box** (DEPLOY.md). The Mac gave 57% uptime in the
  first logged week.
- [ ] **Ledger notes for core-btc-pre2017-v1** (the process note and the reading), if they
  haven't been added yet.

## Later

- [ ] **Point-in-time universe** - the backtests use today's most traded coins, which flatters the
  satellite (survivorship). Done when: a universe built from volume ranks as of each date
  (including delisted coins) exists, and any use of it in a verdict goes through a registered
  protocol.
- [ ] **The ML filter in the reconciliation** - inactive now. If it's ever enabled, the
  reconciliation can't reproduce the signals it filters.

## Done

- [x] Universe files save the full strategy selection; frozen reruns use the saved one, or
  recover it from the activity log (2026-10-04)
- [x] A failed learn is retried an hour later, not a week later (2026-10-04)
- [x] Go-live check: every protective exit rests on the exchange (2026-10-04)
- [x] `tradebot weekly`, CLAUDE.md, BACKLOG.md, STATUS.md, DEPLOY.md (2026-10-04)
- [x] The account replay follows its own positions; slots are freed when a position is flat (4e812f4)
- [x] Uptime with causes, scan log, strict running test; the settings hash ignores timeframe order (4e812f4)
- [x] Paper vs backtest reconciliation (5cd9138)
