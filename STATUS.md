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
