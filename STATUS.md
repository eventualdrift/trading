# Status

One entry per session or weekly check-in, newest last. Each is under a page: what changed, what
the numbers say (with reproduce lines), what needs the owner, what's next.

## 2026-10-04 - session: frozen selection, weekly check-in, agent workflow

Code feb0d5c (branch claude/loving-cannon-5lovc2)

**What changed**
- feb0d5c: universe files save the whole strategy selection, and frozen reruns use it
  (`--current-selection` for today's). Older files get theirs back from the bot's activity log.
  `tradebot weekly` added. A failed learn is retried after an hour. Go-live check: every
  protective exit rests on the exchange (fails today). CLAUDE.md, BACKLOG.md, DEPLOY.md added.
  The docker container runs as the host user and records its commit.
- Earlier this week: 4e812f4 (the account replay follows its own positions; uptime with
  causes; scan log), 5cd9138 (paper vs backtest reconciliation).
- No change to what the bot trades or what learn selects. Paper continues at 65/35.

**What the numbers say**
- No new real-data numbers in this session. The five blocks' outputs from the 10-03 round
  didn't reach this session, so the 385 bps exit and block 3's selection warning are still
  open (BACKLOG.md).
- Tests: 266 pass (full suite, and in a clean clone before the push).

Reproduce (on the Mac or the box, with this code):

    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@389af86-178b23c9a0.json
    tradebot reconcile --since "<since>" --end "<end>"   (from the last report's "Reproduce:" line)

**What needs you**
- The outputs of the five blocks (or the agent reading `reports/` on the box), to name the
  385 bps exit.
- Rerun block 3 with this code. Its "Strategy selection used" line should say the selection
  was recovered from the activity log. Then pick the new stamped file as the weekly reference.
- Go-live item recorded, for your decision: every protective exit must rest on the exchange.
- The move to the always-on box (DEPLOY.md) is yours to do. Code reaches the bot only when you
  say so.

**What's next** (BACKLOG.md)
- Confirm the replay-fix rerun used the 09-27 selection
- Attribute the 385 bps exit
- Explain the 09-30 daily close

## 2026-10-07 - session: restated satellite finding, ML filter, one settings hash, agent separation

Code df433c8 (branch claude/loving-cannon-5lovc2)

**What changed**
- df433c8:
  - every report hashes the config file's settings as loaded, with the run's timeframes and core
    fraction beside the hash;
  - the agent's checkout writes only to its own state and data, and opens the bot's database
    read-only (`observe_state_dir`); `run` and `learn` refuse there;
  - the reconcile shows the strategies in force with the ML flag, what the ML filter blocked and
    passed and what the backtest says those signals were worth, why the backtest had no signal
    for each paper-only row, and the cause of each core target mismatch;
  - `tradebot report` names the ML model in force; the bot logs the model's threshold and
    training end, and the candle behind each core target.
- CLAUDE.md: your two new hard rules, the 0.1-Sharpe discipline line, and edits to the hard rules
  or the stop-and-ask list now need you. DEPLOY.md step 6: the agent's own state and data, and the
  research ledger moves to the agent's checkout.
- No change to what the bot trades or what learn selects. Paper continues at 65/35.

**What the numbers say** (your runs on ae152bd)
- Corrected replay on the same coins, data, selection (recovered from the activity log) and
  settings as the 389af86 run. Like for like from 2023-01-14, 65/35 vs core at the same exposure:
  - Sharpe 1.07 vs 1.12 (was 1.05);
  - +28.5% vs +30.9% a year (was +27.7%);
  - worst dip -24.3% vs -24.3%;
  - satellite Sharpe 0.50 vs a break-even of 0.52 (was 0.41 vs 0.54).

  Since 2022: Sharpe tied, worst dip 0.5 points shallower.
- **Restated finding: no measurable effect of the satellite either way.** "Costs about 3 points a
  year" is about 2 like for like, which is inside the swings implementation details alone have
  produced. The case for a larger core is simplicity and the same-regime structure, not a measured
  cost.
- Ranking: with every signal counted, taken (+0.074R) and skipped for lack of room (+0.078R) are
  equal on average. Low priority; it needs untouched data.
- Reconcile 09-27 to 10-03: one first difference, the rest knock-on. The 385 bps exit was PUMP
  momentum@4h's trailing stop at 09-27 16:00: the Mac slept 15:28-16:53, paper sold on waking, and
  +0.27R became -0.12R. The 09-30 signals came while the bot was not running. The ML filter was on.
- Uptime 24% over 09-27 to 10-03 and 16% over the first weekly: 25 gaps, all the computer asleep.
  Paper numbers are not comparable until the move.

Reproduce:

    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@ae152bd-178b23c9a0.json
    tradebot reconcile --since ... --end ...   (09-27 to 10-03: the exact times are in that report's "Reproduce:" line)

**What needs you**
- **The conclusion moved** (a result that changes a conclusion): the satellite finding is restated
  above, for the record and the planning chat. The split before live is your decision.
- The ML filter is on: paper filters and sizes signals that the backtest takes at 1x. The decision
  is yours; the numbers come from the next reconcile (BACKLOG a).
- If the two core mismatches turn out to be failed fetches (BACKLOG c), retrying that day's
  rebalance would change the core's trades. Your call.
- Use the new DEPLOY.md step 6 for the move.

**What's next** (BACKLOG.md): a, b and c all need one reconcile with this code on the bot's data
(the Mac now, or the agent on the box).
