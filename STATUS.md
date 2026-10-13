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

## 2026-10-10 - session: hands-free, the gates that replace approvals

Code 71f9573. The owner's decision of 10-10: the agent runs the project hands-free, and gates
replace approvals. This cloud session built the gates on the owner's instruction (after the 10-07
handoff); the VM session runs them from here. `git pull` in `~/tradebot-agent` first.

**What changed** (71f9573)
- **CLAUDE.md rewritten**: the owner's hard rules, plus three new ones (never tune on results;
  paper runs only what the backtest models; retraining on the weekly schedule only). Then the
  four gates, the fixed protocol template with defaults per family, and the shorter stop-and-ask
  list (the hard rules, a gate that can't resolve). The agent-separation and ledger rules of 10-07
  are kept, under "Working rules": the owner's restated hard-rule list didn't name them.
- **Gate 1**: `tradebot gate same-trades` replays the live path through the real `TradingBot` for
  40 days on a fixed dataset stored in `tradebot/gates/data/`: 4 coins, 300 days of 1h candles,
  sha256 03ea1f0d. It uses momentum@4h, breakout@1d and breakout@1h at 65/35, and compares every
  order with `tradebot/gates/golden.json`, generated from 4d523da. `tradebot gate deploy` runs
  lint, the suite, the fresh-clone suite and same-trades. It compares the golden orders with those
  of the commit `~/tradebot` runs, then fast-forwards it, rebuilds and checks the container.
- **Gate 2**: `tradebot research register / run / record / protocols` (`tradebot/protocols.py`).
- **Gate 3**: `tradebot gate drawdown` (`tradebot/gates/drawdown.py`); `--set-limits` once.
- **Gate 4**: `tradebot notify`, `weekly --notify`, `scripts/weekly.sh`, `daily.sh`,
  `install-cron.sh`.
- **The ML filter and its 1.5x sizing are off in code** (owner's order): `ML_IN_REPLAY = False`
  in `tradebot/config.py`. Live loads no model and learn trains none, whatever `ml.enabled` says.
  `report` prints "ML filter: OFF - switched off until the account replay models it".
- **Core rebalance day** (owner's order): fetch every core price and the day's candle first. Move
  nothing until all are there, asking again every 5 minutes for up to 6 hours after the close;
  then go ahead with what's there. The activity log gets `core_retry` (once a day) and
  `core_day.incomplete`; Telegram gets an error message.
- **Lint**: the old E741/E731 findings are fixed by renames. Ruff's rule set is pinned in
  pyproject.toml (E4, E7, E9, F) so every ruff version agrees, and ruff is in the dev extras (the
  deploy gate runs `python -m ruff`).
- Tests: 293 pass (repo and clean clone).

**What the numbers say**
- Same-trades on this code: "identical to the golden file: 51 positions (49 closed), 260 signals,
  9 core trades". Reproduce: `tradebot --config none.yaml --env none.env gate same-trades`.
- The lint renames don't affect trades: the golden file came from 4d523da, before them.
- The core fix leaves the golden orders identical: the replay never fails a fetch. In the tests,
  a daily feed 2 hours late gives one `core_retry` and no trades until the day's candle arrives.
  Then the targets come from that candle. A feed more than 6 hours late gives a rebalance with
  what's there, logged `incomplete`.
- The ML switch-off also leaves the golden orders identical (the replay never had the filter).
  It still changes paper's trades whenever a promoted model exists (the 09-27 to 10-03 reconcile
  found one). It goes in under the owner's order, not as "not trade-affecting".
- No numbers from the bot's data this session: this container can't reach the VM. BACKLOG a, b
  and c need the VM session.
- Choices inside the gates, made now and fixed:
  - the template's pass rule: at least +0.10 Sharpe (the size of implementation swings), a dip
    no more than 10% deeper, at least 100 satellite trades or 3 years of days;
  - the range check: 10-day blocks of the backtest's daily returns, 2000 resamples; in range when
    the backtest does at least as badly 5% of the time or more;
  - mechanics are correct when the period's reconcile shows no first differences, no core
    differences and uptime of 95% or more.

  A drawdown hit goes to Telegram only when the agent has work (outside range, mechanics
  differ). In-range hits go in the Monday summary.

**What needs you**
- Nothing to decide. Two things to look at if you want:
  - the protocol template's defaults (CLAUDE.md, gate 2). They are fixed for every protocol, so
    any change should come before the first one is registered;
  - the agent-separation and ledger rules, now working rules. Moving them back under the hard
    rules is yours to do.

**What's next** (the VM session, in `~/tradebot-agent`)
1. `git pull`, the verification block of the 10-07 handoff, then BACKLOG "On the box, first":
   deploy through gate 1, set the drawdown limits from the weekly reference, the agent's `.env`,
   `scripts/install-cron.sh`.
2. BACKLOG a, b, c: `report` and the two reconciles on the bot's data, recorded here and ticked.
3. a2, the ML filter in the replay, then its `ml` protocol.

## 2026-10-12 - weekly check-in (`tradebot weekly`)

Code 391661c · settings 75a58dc389

**What changed**
- code ae152bd -> 391661c: 391661c Owner's CLAUDE.md edits; review fixes for the gates; late core days explained; a6e31ae CLAUDE.md for the hands-free regime; BACKLOG and STATUS for the gates; 71f9573 Gates that replace approvals; ML filter off; core waits for the day's candle; 4d523da STATUS.md: handoff to the local session on the VM; 95a589f STATUS.md: session entry for 2026-10-07; df433c8 One settings hash; agent reads the bot read-only; ML and mismatch reporting
- setting data.dir: saved 'data', now '/home/tradebot/tradebot-agent/data'
- learn of 2026-10-04 01:25 (in force from 10-07 23:29): a new ML model
- learn of 2026-10-10 18:43 (in force from 10-10 18:43): added momentum@1h; dropped momentum@4h

**What the numbers say**
- Uptime 79% of the week; 33 gaps: 33 computer asleep (35.8h)
  Most down: Tue 10-06 24.0h, Wed 10-07 9.8h, Mon 10-05 2.0h
- Signals: backtest 88, paper 88, both 88; taken vs skipped agree 81 of 88 (1 first differences, 6 knock-on)
- ML filter on: 68 of 88 scored signals filtered out (as backtest trades: 5 won of 49, total -36.7R); 20 sized up by confidence
  First difference 10-08 16:00 OGN/USDT momentum@4h: a live-only check (daily loss limit reached)
- Fills: 4 shared entries, entry +92.5 bps vs modelled; 4 shared exits, exit -831 bps, R paper - backtest +0.56
- Core: targets match 14 of 14 coin-days; rebalance rule differs on 0 coin-days; 1 trades
- Frozen reference rerun, from the satellite's first trade (2023-01-14):
  65%/35%                           +29.4%/yr, worst dip  -24.3%, Sharpe 1.10
  core only                         +38.9%/yr, worst dip  -30.7%, Sharpe 1.11
  core at same exposure (77%)       +30.9%/yr, worst dip  -24.3%, Sharpe 1.12
  satellite only                     +9.8%/yr, worst dip  -21.1%, Sharpe 0.59
  Strategy selection: saved with the run
- Drawdown gate: account 0.0% (limit 36.5%), satellite 0.0% (limit 31.7%), losing run 0 (limit 11), over 0.0 days, 0 trades
- Full reports: reports/weekly/2026-10-12-2200

Reproduce:
    tradebot reconcile --since "2026-10-05 22:00" --end "2026-10-12 22:00"
    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@ae152bd-178b23c9a0.json

**What needs you**
- Uptime 79% (under 95%): paper can't be compared with the backtest while the bot is down
- 1 first difference(s) between paper and the backtest's decisions
- The ML filter was on in this window: paper filtered and sized signals the backtest doesn't model (it is off in code, config.ML_IN_REPLAY, once that commit is deployed)
- Settings changed since last week: data.dir: saved 'data', now '/home/tradebot/tradebot-agent/data'
- Learn changed the strategy selection (in-sample: informational, not evidence)
- The frozen reference rerun moved since last week (combined Sharpe 1.07 -> 1.10, satellite Sharpe 0.50 -> 0.59): check whether a conclusion changes

**What's next** (BACKLOG.md)
- a. The ML filter: what it did
- a2. The ML filter in the account replay. The replay takes every signal at 1x; with the
- b. The 6 paper-only signals after 10-03 12:00 (paper 51, both 45 in the first weekly).

## 2026-10-12 - session: on the box; gates live; BACKLOG a, b, c from the bot's data

Code 391661c (the box's agent checkout; **not on GitHub**: the box has no push credentials, see
What needs you). The weekly entry above is this Monday's, from the same run that sent the
Telegram summary.

**What changed** (391661c)
- **Deployed through gate 1** at 22:18 UTC (`state/deploys.jsonl`): dry run, then the real run;
  every step passed (lint, suite, fresh-clone suite, same-trades identical: 51 positions, 260
  signals, 9 core trades); the container was rebuilt from 391661c and stayed up 45 s without a
  restart. The bot runs with the ML filter off ("ML filter: not active" in its log) and the core
  fetch-first fix. 4d523da ran before it, since 10-07.
- **CLAUDE.md, the owner's edits**: the agent writes only to its own state and data, and the
  ledger changes only through `tradebot research`, are hard rules again. The protocol template
  names the untouched reserve it spends; a pass holds on each half of the period as well as
  overall; three protocols a quarter. The commands enforce them: `register` refuses a spent
  reserve (the older `core-btc-pre2017-v1` spent pre-2017 BTC) and a fourth protocol in a quarter;
  `record --verdict pass` needs the three numbers at or past the family floor.
- **Review of the gate code** before the deploy (five lenses, one verified finding and sixteen
  judged by hand): the deploy gate checks the bot checkout first, reads the running commit from the
  container, builds before it switches the checkout (a failed build puts it back) and watches the
  container settle; a registered test accepts one golden file (`--accept` is in the ledger); a
  drawdown hit whose Telegram message failed is sent again; `weekly` exits 3 when only the summary
  failed; the cron scripts abort a failed rebase, STATUS.md merges by union, and the backup uses
  the venv's sqlite (the box has no `sqlite3`; the first backup is in `~/backups`: 853 snapshots,
  14 positions in the copy).
- **Core**: the 65/35 split happens at the first core day even while the day's candle is missing
  (before, the wait delayed it and the satellite sized from the whole account meanwhile); the
  error message says how late the close was. The reconcile explains the days the core waited or
  went ahead without data, and takes the first logged selection as in force from its learn, not
  from the restart that logged it: the pre-09-29 windows now replay the right strategies.
- **Drawdown limits set** (`drawdown-limits-v1`, in the ledger): account 36.5%, satellite 31.7%,
  losing run 11 (the reference's like-for-like window from 2023-01-14: worst dips 24.3% and 21.1%
  x 1.5, longest run over 508 trades); watching paper from 2026-10-12 22:18. The first daily run:
  0.0% / 0.0% / 0 over 0 days.
- **Telegram** from the agent's own `.env`: "agent checkout ready" and the weekly summary went out.
  **Cron** installed: Monday 06:17 weekly, daily 06:47 drawdown gate, daily 03:31 backup.
- **Ledger notes**: the replay notes on `sizing-open-risk-budget-v1` and `core-robustness` (both
  judged under the pre-4e812f4 replay; verdicts stand), and the process note and reading on
  `core-btc-pre2017-v1`.
- Tests: 298 pass (repo, fresh clone, and the deploy gate's own runs).

**What the numbers say** (from the bot's database, read-only; windows reproduced by the lines below)
- **a. The ML filter.** The learn of 10-04 01:25 UTC promoted a model (threshold 40%, trained to
  10-03); the filter was on from 10-04 02:25 until the deploy. The 10-10 learn kept it (t=0.76
  < 2.5) and re-selected momentum@1h in place of momentum@4h. 09-27 to 10-03 12:00: off, nothing
  scored. 09-28 to 10-05: 10 signals scored, 6 filtered out, 4 passed, 4 sized up x1.03 on
  average (their backtest trades were still open at the data end). 10-04 to 10-12: 89 scored,
  65 filtered out (as backtest trades: 5 won of 32 completed, -21.6R in total), 24 passed (8 won
  of 15, +10.2R), 24 sized up x1.06 on average, x1.16 at most. Reading: eight days of candidate
  trades, not evidence either way; the path is a2 (model it in the replay), then an `ml`
  protocol.
- **b. The six paper-only signals** (first weekly window, paper 51, both 45): 10-04 00:24 GLMR
  momentum@1d, GLMR breakout@1d, STRK momentum@1d, STRK breakout@1d, STRK momentum@4h; 10-04
  12:36 BEAMX momentum@4h. All six are candles that closed at 00:00 / 08:00, scanned 24-36 minutes
  later with a coin list refreshed on waking: GLMR and STRK entered the top 30 at 00:24, BEAMX at
  12:36. The reconcile keys the backtest's list at the candle close, so it doesn't replay them
  ("not replayed"). Not a bot fault; the reconcile refinement is in BACKLOG. The 10-04 learn
  did not change the selection (the same three combos); the 10-10 learn did.
- **c. The two core coin-days** (10-04 BTC and ETH, "paper 1.0 vs backtest None"): the backtest
  side had no 10-04 daily candle in the Mac's run of 10-05 00:00; the same window rerun here
  gives 12 of 12, 09-27 to 10-03 gives 8 of 8, 10-04 to 10-12 18 of 18, the weekly 14 of 14.
  Paper's targets were right. The fetch-first fix is deployed regardless: the failed-fetch case it
  covers is real on any restart.
- **The frozen reference rerun moved** (weekly: combined Sharpe 1.07 -> 1.10, satellite 0.50 ->
  0.59). Cause found: ae152bd and 391661c on the box's data give the same numbers to the end
  value (2,594 and 1,411), so the code didn't move it; the candle cache on the box (copied after
  the bot's 10-10 learn) differs from the Mac's of 10-05 before the data end. The restated
  finding stands: 65/35 vs the core at the same exposure, Sharpe 1.10 vs 1.12, worst dip equal,
  "does not beat it"; the break-even line flipped to yes (0.59 vs 0.52), within the 0.1 that
  implementation details move. The agent's `data/` is the reference cache from now on; pinning
  the candles in universe files is in BACKLOG.
- **This week on the box** (the weekly entry above): uptime 79%, all of it the Mac's last days
  (10-05 to 10-07); 10-08 to 10-12 have no gaps. One first difference: the daily loss limit, a
  live rule the replay doesn't model (BACKLOG, fidelity).
- Reproduce (from `~/tradebot-agent`, `--config config-agent.yaml`):
    tradebot reconcile --since "2026-09-27 00:00" --end "2026-10-03 12:00"
    tradebot reconcile --since "2026-09-28 00:00" --end "2026-10-05 00:00"
    tradebot reconcile --since "2026-10-04 00:00" --end "2026-10-12 00:00"
    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@ae152bd-178b23c9a0.json
    tradebot gate same-trades
    tradebot gate drawdown

**What needs you**
- **Push credentials on the box** (gate 4 can't resolve this): `~/tradebot-agent` has an https
  remote and no deploy key, credential helper or `gh` login, so nothing pushes from here: not
  this session's commits (391661c and the STATUS commit), not Monday's STATUS.md. DEPLOY.md step
  3 (a deploy key with write access, the `github-tradebot` host alias, the remote switched to it)
  fixes it. Until then GitHub is at a6e31ae and the box is ahead.
- **The daily loss limit and the max-drawdown halt** (hard rule: paper runs only what the backtest
  models). Both are live rules the account replay doesn't model; the 10-08 OGN signal was skipped
  by the first. My recommendation: model them in the replay (BACKLOG, next up; a fidelity fix on
  the backtest side) rather than switch off risk controls meanwhile. Say if you want them off
  until then; otherwise they stay on and the replay catches up.

**What's next**
1. The daily loss limit and the drawdown halt in the account replay, then a frozen rerun and
   ledger notes.
2. The reconcile's coin list at the scan; pin the candles in universe files.
3. a2: the ML filter in the replay, then its `ml` protocol.
4. The same-trades dataset extended to stale candles, fetch failures and a model in use.

## 2026-10-13 - session: the risk controls modelled, candles pinned, the ML filter replayed

Code db4371d deployed (HEAD two small commits ahead, agent-side only) · settings 75a58dc389 (unchanged)

**What changed**
- CLAUDE.md (owner, 2026-10-13): "paper runs only what the backtest models" now keeps components
  that only reduce risk (the daily loss limit, the max-drawdown halt) on, and the replay models
  them first. Decisions taken as sent: the controls stayed on throughout.
- 2362e5f: the account replay applies both breakers on its own equity path, valued at each signal
  from the last closed candle of the finest timeframe replayed (live values from the last price);
  the daily limit is checked at each signal against the day's 00:00 UTC equity, as live does; a
  halt is resumed at the next daily close with the peak restarted, as /resume does. Window
  replays (the reconcile) start from paper's open positions at paper's own sizes and realised
  outcomes; a breaker disagreement is named with both sides' numbers. Universe files pin the
  candles a run loaded (per coin and timeframe: first and last candle, rows, hash); a frozen
  rerun says "identical" or lists what differs; `weekly --set-reference` switches the reference
  without a check-in. The weekly's Telegram summary and STATUS entry lead with learn's selection
  changes.
- b068c25: BACKLOG a2 - the ML filter replayed walk-forward (`portfolio-backtest --ml-replay`):
  one learn a week from the data start, each trained only on signals whose exit candle had closed
  by then, promoted only through the live gate; the model in force scores, filters, sizes (the
  confidence multiplier) and ranks (expected R) each later signal as the live scan does. A test
  proves no learn sees a later label. Plus the first review's eleven findings (all confirmed,
  all fixed): seeds carry paper's outcome; the peak is walked at every candle close; the walk
  starts at the first signal; an operational halt is not the drawdown halt; a saving run loads
  every frame to one data end so pins match a frozen rerun; a kept stamped file is checked
  against this run's candles; switching the reference no longer reports a false "moved".
- db4371d: the second review's findings: the learns train on every configured timeframe (15m,
  1h, 4h, 1d) as live's learn does; an outcome counts once its exit candle has closed; under the
  open-risk budget a signal is admitted on its 1x risk and then sized up; the window-end "no
  next candle" row is a note, not a disagreement; a daily-limit disagreement quotes paper's
  equity at the scan that decided.
- Deployed through gate 1: db4371d at 11:29 UTC (lint, the suite, the suite in a fresh clone, same-trades
  identical to the golden file, same orders as the running commit: not trade-affecting; the
  container ran 45 s without a restart). The bot has run db4371d since; the two commits after it
  (rerun flag and messages, tests) change nothing live and wait for the next deploy.
- The weekly reference is now `reports/universe-20260927-1513@b068c25-75a58dc389.json` (the same
  run stamped at b068c25 with the box's candle pins: 62 coin-timeframes); Monday's entry reports
  the switch. Ledger notes through `research note`: sizing-open-risk-budget-v1, core-robustness
  and drawdown-limits-v1 (the fidelity fix and the reference's move), ml-replay-a2 (information).

**What the numbers say** (`--config config-agent.yaml` from `~/tradebot-agent`; reproduce lines below)
- **The reference with the breakers**, from the satellite's first trade (2023-01-14): combined
  +29.3%/yr, worst dip 24.5%, Sharpe 1.10 (was 1.10); core only 1.11; core at the same exposure
  1.12; satellite +9.5%/yr, worst dip 21.7%, Sharpe 0.58 (was 0.59). The daily loss limit blocked
  nothing in 3.7 years (1% risk per trade and three slots: a 3% day takes three stops); the halt
  fired twice (2024-05-09 and 2026-02-22, each resumed the next day), blocking 6 signals. The
  limits the drawdown gate would set from this replay: account 36.8%, satellite 32.6%, losing run
  11, against the set 36.5% / 31.7% / 11; the set ones stand (changing them is a registered test)
  and are the tighter.
- **Why paper hit the limit on 10-08 and the backtest never does**: RUNE (opened 09-27 00:00, the
  first candle) was sized from the whole 1,000 before the first sleeve split moved 650 to the
  core, so its stop cost 3.2% of the satellite; every later position is 1% at risk. The window
  replay now seeds paper's positions at paper's sizes, so 10-04 to 10-12 agrees on 88 of 92
  shared signals (was 87): OGN 10-08 16:00 is blocked on both sides. The one first difference
  left is OGN 10-08 20:00: paper opened it (its equity was -2.9% against the day's start at
  19:58, having been -3.1% at 19:28), the replay still had it under the limit at the 20:00
  candle close. A knife edge, now explained in the report line; the 10-12 00:00 S/USDT row is
  the window's end, no longer counted.
- **The ML filter replayed on the reference** (information, not evidence - the strategies were
  selected on this data): with the learns trained as live trains them (every
  configured timeframe), 260 weekly learns, 5 promotions (the first 2025-04-30), 2,320 signals
  scored, 1,114 filtered out (as backtest trades 386 won of 1,114, -111R in total), none sized
  up; from the satellite's first trade the satellite's Sharpe is 0.38 with the filter against
  0.58 without, the combined account 1.05 against 1.10. A first run whose learns saw only the
  replayed timeframes (4h, 1d) had given 0.64 with: the sign flips with the training set, so
  this is exactly what an `ml` protocol on untouched data must decide, not a finding either
  way (ledger: ml-replay-a2, two notes).
- **Candle pins**: the box's cache starts before the reference's window for every established
  coin; the 20 coin-timeframes that start later are listings younger than the window (TAO, ENA,
  SAGA 2024; ONDO, PUMP, XPL 2025; SUI, WLD, ARB, PEPE 2023 on 1d). So the rolling fetch start
  truncated nothing on the box, and the box's rerun gives 2,587 / 1,400 to the end value whether
  pinned or not; the Mac's cache of 10-05 can't be checked after the fact. From now on a weekly
  rerun on a refilled cache says so, per coin and timeframe.
- Reproduce:
    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@b068c25-75a58dc389.json
    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@b068c25-75a58dc389.json --ml-replay
    tradebot reconcile --since "2026-10-04 00:00" --end "2026-10-12 00:00"
    tradebot gate deploy --dry-run

**What needs you**
- **Push credentials** (still): `gh auth status` says not logged in for the tradebot user as of
  12:08 UTC; nothing pushed. GitHub is at a6e31ae; the box is at db4371d plus this entry's
  commit. The Monday cron (10-19 06:17 UTC) will fail its push and say so until this is done.
- **The halt's resume**: the replay assumes you send /resume by the next daily close (the peak
  then restarts, as the command does). If a halt would stay longer in practice, say so and the
  replay models that instead.
- A residual from the review, no action unless you want it: live shifts the breakers' peak and
  day-start by the amount moved between sleeves, which changes the distance to both thresholds by
  a few tenths of a percent around each reset; the replay's normalised path doesn't. Modelling it
  in the replay is in BACKLOG; making live ratio-preserving would be a live change (yours).

**What's next** (BACKLOG.md)
1. Sizing from the marked equity in the replay (live sizes from equity with open positions at the
   last price), then the frozen rerun and ledger notes.
2. The reconcile's coin list at the scan (the first weekly's six paper-only signals).
3. The same-trades dataset extended to stale candles, fetch failures and a model in use.
4. The `ml` protocol once the reserve holds 100 satellite trades after 2026-09-27.
5. Monday 10-19: the first cron weekly; its push once credentials exist.

## 2026-10-13 - session (continued): sizing from the marked equity; the coin list at the scan

Code 21aab7f deployed; HEAD c20754a (the window-start anchor, agent-side) · settings 75a58dc389 (unchanged)

**What changed**
- 01a7fcd: the account replay sizes and caps each position from the satellite's equity at the
  signal with open positions at the last close (live sizes from its equity at the last price);
  before, from realised equity alone. The reconcile keys paper's coin list and the strategies in
  force at the logged scan, not at the candle close plus a fixed lag: a coin that joined the list
  when the bot woke is judged by the replay. Review (three findings, all confirmed): window seeds
  are booked on the window-start base (733cde5); the tests pin a late scan and the marked-equity
  room and budget (af93fa3).
- Deployed through gate 1: 21aab7f, gate 12:28 to 12:42 UTC (lint, the suite, the suite in a fresh clone,
  same-trades identical to the golden file, same orders as the running commit: not trade-affecting;
  the container ran 45 s without a restart). c20754a after it: a window replay starts worth exactly
  1.0 (the seeds' unrealised P&L is in paper's base already) - replay-side only, waits for the next
  deploy.
- Ledger notes (fidelity, the sizing fix): sizing-open-risk-budget-v1, core-robustness,
  drawdown-limits-v1. BACKLOG: both items ticked; two residuals stay open (the sleeve-transfer
  shift, the `ml` protocol).

**What the numbers say**
- The reference with sizing from the marked equity (pinned file, candles identical): combined
  Sharpe 1.10, satellite 0.58, worst dips 24.5% / 21.8% - inside rounding of the morning's
  numbers; the gate's limits would be 36.8% / 32.6% / 11 (set 36.5% / 31.7% / 11, unchanged).
- 10-04 to 10-12: 98 shared signals (92 before the scan-time keying), 95 agree (87 of 92 at the
  start of the day); one first difference, OGN 10-08 20:00 - paper's equity at the scan was within
  a tenth of a USDT of its limit (its day's start was set at 00:00:30; the record at 19:58 reads
  -2.9%), the replay's own path was -4.8% under its 00:00 close (its modelled outcomes within the
  window and the day-start base differ from paper's). The window-end row is a note now, not a
  difference.
- 09-28 to 10-05 (the first weekly window): 51 shared signals (45 before), 47 agree, 1 first
  difference, 3 knock-on, paper-only 0: the six signals that joined the list at the scan are
  judged by the replay now.
- Reproduce:
    tradebot portfolio-backtest --core-fraction 0.65 --universe-file reports/universe-20260927-1513@b068c25-75a58dc389.json
    tradebot reconcile --since "2026-10-04 00:00" --end "2026-10-12 00:00"
    tradebot reconcile --since "2026-09-28 00:00" --end "2026-10-05 00:00"

**What needs you**
- As in the entry above: push credentials (none at 12:54 UTC); the halt's resume assumption.

**What's next** (BACKLOG.md)
1. The same-trades dataset extended to stale candles, fetch failures and a model in use.
2. Live vs backtest candle history (needs four weeks above 95% uptime; the box has two days).
3. The sleeve-transfer shift (low), the `ml` protocol when the reserve has 100 trades.
4. Monday 10-19: the first cron weekly, on the pinned reference; its push once credentials exist.
