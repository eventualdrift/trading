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

## 2026-10-07 - handoff: the cloud session ends, the local session on the VM takes over

Code df433c8 (the last code change); this entry is the last commit from the cloud session, which
stops pushing to `claude/loving-cannon-5lovc2` after it. From here the branch belongs to the
local session in `~/tradebot-agent`, under CLAUDE.md. This entry is longer than a page on purpose:
it is the handoff.

**What changed**
- Nothing in code since df433c8; this entry only. Tests: 274 pass (repo and clean clone).

**In flight (BACKLOG a-e)**
- **a. ML filter** - the reporting is built. Still needed: `report` and one reconcile on the bot's
  data, to record whether the 10-03 learn promoted a model, how many signals the filter blocked,
  and their outcomes. a2 (modelling it in the replay) waits on the owner's decision below.
- **b. Six paper-only signals** - built (each row now says why the backtest had none). Needs the
  same reconcile. Likely part of it: paper keeps scanning coins it holds after they drop out of
  the coin list, and records their signals ("skipped: already in a position"). The replay
  doesn't replay those ("not replayed").
- **c. Core targets 10 of 12** - built (each mismatch gets a cause). Needs the same reconcile.
  The bot only logs the closes behind its targets from df433c8 (the VM bot's first start), so for
  10-03 to 10-05 the report can name "no daily candles at its check" but not a close difference.
- **d. One settings hash** - done (df433c8). Every report now hashes the config file as loaded;
  expect 7c350d35dc for the current config. All hashes differ from reports before df433c8
  (178b23c9a0 was the old run-specific definition).
- **e. Agent separation** - done (df433c8). Verify it on the VM with the commands at the end.

The Mac commands I asked for, and why (run them on the VM, from `~/tradebot-agent`):
1. Update and restart: superseded. The VM bot starts on the branch head, so its new logs (ML
   model details, the core's closes) are on from its first start. Don't redeploy unless the owner
   says so.
2. `.venv/bin/tradebot --config config-agent.yaml report` - which ML model is in force, and whether
   the 10-03 learn promoted one (goes under a, and "What changed").
3. `.venv/bin/tradebot --config config-agent.yaml reconcile --since "..." --end "..."` twice: for
   09-27 to 10-03, and for the 10-05 weekly window. The exact times are in the "Reproduce:" lines
   of the owner's reports, copied to `~/tradebot-agent/reports/` (the reconcile report from that
   round, `reports/STATUS-mac.md` and `reports/weekly/<date>/reconcile.txt`). This answers a, b
   and c: record them in STATUS.md and tick them in BACKLOG.md.

**Owner decisions waiting**
1. **The ML filter and its 1.5x sizing.** The filter is on whenever a promoted model file exists.
   A learn that doesn't promote a new model keeps the old one. With it on, paper differs from the
   backtest in three ways:
   - signals scored below the threshold are dropped;
   - trades are sized up to 1.5x (`risk.max_risk_multiplier`, when the model's report allows
     confidence scaling);
   - signals at the same close are ranked by the model's expected R instead of by reward:risk.

   The options:
   - **Off**: from `~/tradebot-agent`, run
     `.venv/bin/tradebot --config ~/tradebot/config.yaml config-set ml.enabled=false`, then
     `cd ~/tradebot && docker compose restart tradebot`. No model is loaded: every signal is a
     candidate at 1x, ranked by reward:risk, which is what the backtest models. Learn stops
     training models; its strategy selection is unchanged.
   - **Filter on, no sizing**: `config-set risk.max_risk_multiplier=1` and a restart. Filtering
     and ranking stay; every trade is 1x.
   - **Keep both**: build a2 (filter, sizing and ranking in the replay), then judge it with a
     registered test on untouched data.
2. **Core rebalance-day fix** (only if c confirms failed fetches). Today `_maybe_core_day` in
   `tradebot/bot.py` marks the day done (`core_last_day`) before it fetches prices and targets.
   If the fetch fails (for example on waking), that day's rebalance is skipped, and a sleeve reset
   that day values holdings without prices.
   - The change: fetch prices and targets first. If any core coin lacks either, and the day's
     close is less than 6 hours old, log it and return without marking the day; the next tick
     retries. After 6 hours, go on with what's there (as today). Initialization and sleeve
     resets come after the check, so a retry can't run them twice.
   - Effect: on such days the core rebalances at the first complete check, minutes later at that
     moment's price, instead of a day later. That is closer to the backtest's daily rebalance.
     Days whose first check succeeds don't change.
   - The test: a market whose first daily fetch fails and then succeeds makes the same trades as
     `simulate_core`.

   Expect it to be rare on an always-on VM.

**What isn't written down anywhere else**
- **Working with the owner.**
  - Decisions arrive as numbered items from a planning chat. Reply in plain language, with
    paste-ready commands, one step per block.
  - The standing line: no setting changes, paper continues at 65/35.
  - Attached outputs sometimes don't arrive. Ask for the exact line; never guess numbers.
- **Shell.**
  - On the owner's Mac (zsh), `!` inside double quotes breaks commands, and so do placeholders
    like `<file>`, which zsh reads as a redirect. Give commands that find files themselves.
  - When writing a file whose text contains a heredoc, use the editor tool or a unique delimiter.
    A nested `EOF` once ended my heredoc early and ran lines of a doc as commands (it set a local
    git identity, which I removed before committing).
- **The gate takes 5-11 minutes**, so run it in the background. The clean-clone run:
  `git clone -q . /tmp/c && PYTHONPATH=/tmp/c .venv/bin/python -m pytest -q`. Lint findings that
  predate this work: E741 in `bot.py` twice, `engine.py` and `tests/test_engine.py` (three), and
  E731 in `portfolio.py`. Leave them.
- **Activity-log coverage**, which decides what the reconcile can explain:
  - coin list, selection, core days, scan errors: from 5cd9138's restart (09-29);
  - scans, stale-candle skips, starts, stops, pauses: from 4e812f4's restart;
  - ML model details and the core's closes: from df433c8 (the VM).

  Earlier windows are approximated, and the reports say so.
- **The weekly reference**, `reports/universe-20260927-1513@ae152bd-178b23c9a0.json`, holds the
  full selection but the older settings format. Reruns say "older format", compare without
  timeframes, and stamp a copy under the new hash. That hash difference is not a settings change.
  Keep the @ae152bd file as the reference: `weekly.json`, which moved with `state/research`,
  remembers it.
- **Weekly runs only in the agent checkout.** It appends to that checkout's STATUS.md, and a
  modified tracked file in the bot's checkout would block `git pull`.
- **Two replays, on purpose.**
  - The account replays use every signal and flat times: portfolio-backtest, research, project,
    reconcile, and the account lines of `tradebot backtest`.
  - Selection and the per-strategy statistics use the one-trade-at-a-time backtest. Changing that
    would change what learn selects.

  The per-strategy "portfolio return/drawdown" in learn's summary is informational only.
- **Flat-time refinement** uses the shortest timeframe loaded for the same coin. With 4h and 1d
  selected, a stop inside a daily candle is placed by the 4h candles; a 4h stop isn't refined.
- **Reading the bot's database read-only** still needs write access to its `-wal` and `-shm`
  files. So the container must run as the agent's user (DEPLOY step 5 sets `TRADEBOT_UID`/`GID`).
  An "unable to open" or "readonly database" error on a read means a file ownership problem in
  `~/tradebot/state`.
- **Docker builds need `GIT_COMMIT`** (DEPLOY step 5). Without it, reports from the container show
  the code as "unknown".
- **pandas 3**: `Timestamp.value` is in nanoseconds, and indexes can be in milliseconds. Use
  `timeframes.index_ms` and `portfolio._ns`.
- **The ledger.**
  - `sizing-open-risk-budget-v1`: FAIL, under the old replay. Its replay note was meant to be
    added in the 10-04 round (block 2).
  - `core-robustness`: the same replay note.
  - `core-btc-pre2017-v1`: PASS.

  Check with `research oos-show` and the ledger that those notes exist. If the OOS process note
  and reading are missing, they are the owner's to add (BACKLOG, "Needs the owner"). The ledger
  changes only through research commands.
- **Half-done ideas, all in BACKLOG:**
  - a2, the ML filter in the replay, including its ranking;
  - live candle history (at most 1000 candles) vs the full history, counted only in weeks with
    uptime above 95%;
  - a point-in-time universe;
  - ranking by validated edge (low priority).

  Learn can promote a new ML model any Saturday; the weekly flags it.

**config-agent.yaml (exact, for the user `tradebot`; DEPLOY step 6 writes it with $HOME)**

    extends: /home/tradebot/tradebot/config.yaml
    state_dir: /home/tradebot/tradebot-agent/state
    data:
      dir: /home/tradebot/tradebot-agent/data
    observe_state_dir: /home/tradebot/tradebot/state
    learning:
      follow_state_dir: /home/tradebot/tradebot/state

**First commands for the local session** (in `~/tradebot-agent`, after DEPLOY steps 1-6):

    .venv/bin/python -c "
    from tradebot.config import load_config
    from tradebot.cli import _bot_db
    cfg = load_config('config-agent.yaml', None)
    print('own state', cfg.state_path, '| own data', cfg.data.dir)
    print('bot db', cfg.bot_db_path, '| bot strategies', cfg.brain_path)
    db = _bot_db(cfg)
    print('read-only:', db.readonly, '| snapshots read:', len(db.snapshots(cfg.mode)))
    try:
        db.log_bot(0, cfg.mode, 'probe', {})
        print('PROBLEM: the write went through - stop and tell the owner')
    except Exception as e:
        print('write refused:', e)
    "
    .venv/bin/tradebot --config config-agent.yaml run      # must exit: "This config observes another bot ... it never runs a bot."
    .venv/bin/tradebot --config config-agent.yaml learn    # must exit: "This instance uses the strategies in /home/tradebot/tradebot/state ..."
    .venv/bin/tradebot --config config-agent.yaml report   # the bot's track record, the ML model in force, the go-live checklist

The expected results: own state and data under `~/tradebot-agent`, the bot's database and
strategies under `~/tradebot/state`, `read-only: True`, the probe refused ("attempt to write a
readonly database"), and both `run` and `learn` exiting. If any of these differ, stop and tell the
owner before running anything else.

**What needs you**
- The two decisions above: the ML filter and its sizing, and the core rebalance-day fix once c
  confirms the cause.
- Whether the core-btc-pre2017-v1 notes are in the ledger.

**What's next** (for the local session)
1. The verification above, then `report`, then the two reconciles: record a, b and c in
   STATUS.md and tick them.
2. `tradebot --config config-agent.yaml weekly` once the VM bot has run a few days, then the cron
   (DEPLOY step 8).
3. The candle-history item, after four weeks with uptime above 95%.
