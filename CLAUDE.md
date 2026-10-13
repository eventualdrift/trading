# Working on tradebot

A crypto trading bot that is **paper trading**: a 65/35 core/satellite account (BTC/ETH trend
core, signal-strategy satellite). **The agent runs this project hands-free.** Approvals are
replaced by gates: a change goes live in paper when its gate passes, and the owner hears about
it in STATUS.md and on Telegram. The owner keeps the hard rules and the go-live decision.

## Hard rules (owner-only)

- **Paper only.** Never `mode: live`, never exchange API keys, never `--force-live`, never real
  orders.
- **Never commit secrets, state, data or reports**: `.env`, `config*.yaml`, `state/`, `data/`,
  `reports/`.
- **Never edit the hard rules or the stop-and-ask list.**
- **Go-live is the owner's decision.**
- **Never tune on results.** Never choose or alter strategies, parameters, thresholds, filters,
  sizing or the split because of recent performance. The only path to changing what the bot
  trades is a registered test, on untouched data, that passed (gate 2).
- **Paper runs only what the backtest models.** Any component live uses that the account replay
  can't reproduce is switched off until it can, except components that only reduce risk (the
  daily loss limit, the max-drawdown halt): those stay on, and the replay is made to model them
  first.
- **Retraining happens on the weekly schedule only.** A drawdown never triggers a retrain.
- **The agent writes only to its own state and data.** Only frozen runs touch the bot's: its
  database is opened read-only (`observe_state_dir`), its strategies read-only
  (`learning.follow_state_dir`); everything that writes (learn, unfrozen backtests, data
  updates) uses the agent's own `state/` and `data/` (`config-agent.yaml`).
- **The ledger changes only through `tradebot research` commands** (and `gate drawdown`, which
  records its limits and events there), never by hand. Recorded verdicts are never edited or
  rerun.

## Switched off until the replay models them

- **The ML trade filter and its confidence sizing (up to 1.5x)**: off in code,
  `tradebot/config.py: ML_IN_REPLAY = False` (live loads no model, learn trains none, whatever
  `ml.enabled` says). BACKLOG a2 models it walk-forward; then a registered test (template, family
  `ml`) decides whether it comes back. Only a passed test sets `ML_IN_REPLAY = True`.

## The gates

### Gate 1: deploy

`tradebot --config config-agent.yaml gate deploy` (from `~/tradebot-agent`; `--dry-run` checks
without deploying). It deploys HEAD to `~/tradebot` only when every step passes: a clean
checkout; `ruff check tradebot tests`; the full suite; the suite again in a fresh clone of the
commit; the **same-trades check** in that clone; then the orders are compared with the golden
file of the commit running now. Then it fast-forwards the bot's checkout, rebuilds and restarts
the container, checks it runs, and appends to `state/deploys.jsonl`.

- **Same-trades check** (`tradebot gate same-trades`, about a minute): the live path (scanner,
  risk checks, entries, exits, core rebalance) runs through the real `TradingBot` on a fixed
  dataset (`tradebot/gates/data/`) and every order and decision is compared with
  `tradebot/gates/golden.json`. Identical orders: not trade-affecting, it deploys. Different
  orders: it deploys only if the change passed a registered test, recorded in the golden file by
  `tradebot gate same-trades --accept <protocol id>` (refused without a passing verdict).
- Never edit `golden.json` or the dataset by hand. The dataset never changes.
- A live-side fix that only acts when something fails (a fetch, a restart) leaves the golden
  orders identical; it gets its own test showing the failure-free path is unchanged.
- A failed step: fix it and run the gate again. If it can't be fixed, stop and ask (below).

### Gate 2: research

The agent writes and registers protocols itself, from the template below, on untouched data.
`tradebot research register --file p.yaml`, then `research run --id <id>` (once; a second run is
refused), then `research record --id <id> --verdict pass|fail|inconclusive --numbers '{...}'`
(once, from the registered pass rule applied to the run's output). `research protocols` lists
every test with the trial count. A passed test: apply its change, `gate same-trades --accept
<id>`, deploy through gate 1, and note the trial count in STATUS.md. Failed or inconclusive:
recorded, nothing changes; another look is a new id and another trial.

The template is fixed (the owner changes it, not the agent). Every field is required:

```yaml
id: <family>-<short-name>-v<n>   # never reused
family: satellite | core | ml | drawdown
hypothesis: <one sentence: the change, and what it does to the metric>
change: <the exact change: commit on a branch, made before registering, not yet deployed>
data: <coins (universe file), timeframes, start and end dates>
untouched: <why none of this data was used to design or tune the change>
reserve: <the untouched reserve this protocol spends, e.g. "reference universe, candles
          2026-09-27 to 2026-12-27": named once; a reserve spent by one protocol is never
          named by another>
metric: <one number, how it is computed, the line of the run's output that prints it>
pass_rule: <the threshold, decided now; it must hold on each half of the untouched period
            separately as well as overall>
readings:
  pass: <what is applied and deployed>
  fail: <recorded; nothing changes>
  inconclusive: <recorded; what a new protocol would need>
run: tradebot <one command that prints the metric for current and changed side by side,
      overall and for each half of the period>
```

Three rules the commands enforce: **at most three protocols per quarter** (`register` refuses
a fourth); **each protocol names the untouched reserve it spends**, and a reserve is spent once
(`register` refuses a reserve another protocol named, or one containing it; the older
`core-btc-pre2017-v1` spent "pre-2017 BTC"); **a pass holds on each half separately as well
as overall** (`record --verdict pass` needs `overall`, `first_half` and `second_half` in
`--numbers`, each a number at or past the family floor below; the protocol's own stricter rule
and the dip condition are the agent's to apply). Untouched data is finite: three protocols a
quarter, each on its own reserve, is the budget.

Fixed defaults per family (a protocol may be stricter, never looser):

- **satellite** (strategies, parameters, filters, sizing, entries, exits, the universe rule) and
  **core** (its rules, the split): data is only candles after the reference universe file's data
  end, or out-of-sample data never loaded before (as pre-2017 BTC was, before `core-btc-pre2017-v1`
  spent it); at least 100
  satellite trades (satellite) or 3 years of days (core). Metric: the combined account's Sharpe,
  changed minus current, same data and costs. Pass: at least +0.10 and the worst dip no more than
  10% deeper (relative). Fail: 0 or less. Inconclusive: in between, or too few trades/days.
- **ml** (a switched-off component coming back): only after the replay models it walk-forward.
  Data, metric and pass rule as satellite, the replay with the component against without it.
- **drawdown** (gate 3 found paper outside the backtest's range with correct mechanics):
  hypothesis "the live edge is below the backtest's"; data: the paper weeks after the hit, at
  least 8 with uptime above 95%; metric: the share of resampled backtest periods that do at
  least as badly as paper over those weeks (the range check). Pass (confirmed): under 5%.
  Reading pass: recorded and sent to the owner (a go-live matter); any change to what trades
  then needs its own satellite or core protocol. Fail: in range after all, nothing changes.

### Gate 3: drawdown

Limits are set once, in advance, from the reference backtest:
`tradebot gate drawdown --set-limits --reference <universe file> --paper-from <UTC time>`. The
limits: the combined account's and the satellite's like-for-like worst dips x 1.5, and the
longest run of losing satellite trades. Changing them is a registered test. `scripts/daily.sh`
runs `tradebot gate drawdown --notify` every day. On a hit it reconciles the period, checks the
range (a block bootstrap of the backtest's daily returns, or its trades for a losing run, over a
period as long as paper's) and records one outcome in `state/research/drawdown-events.jsonl`:

- **mechanics differ** (reconcile: first differences, core differences, or uptime under 95%):
  fix the mechanics, through gate 1;
- **in range**: recorded, nothing else;
- **outside range** with correct mechanics: a registered test (family `drawdown`).

Trading continues throughout; it's paper. A hit never triggers a retrain or a change.

### Gate 4: reporting

- STATUS.md after every session (output rule below).
- Every Monday `scripts/weekly.sh` runs `tradebot weekly --notify` (STATUS.md entry, Telegram
  summary) and pushes STATUS.md.
- A gate that fails in a way the agent can't resolve: `tradebot notify --text "..."` and the
  item under "What needs you" in STATUS.md.

## Stop and ask

Write the item under "What needs you" in STATUS.md, send `tradebot notify`, stop that thread,
and carry on with anything else on the backlog:
- anything under the hard rules;
- a gate that can't resolve.

Everything else: do.

## Working rules

- **The agent checkout** is `~/tradebot-agent`, with `--config config-agent.yaml` (`run` and
  `learn` refuse there). Code reaches `~/tradebot` only through gate 1; nothing is edited there.
- **Fidelity fixes** make the backtest model what live does. Each is followed by a frozen rerun
  on the reference universe file and a ledger note (`research note --id ... --text ...`) on every
  recorded result it affects.
- **Research discipline.** New rules are judged on untouched data. One run per protocol. Every
  variant tried is a trial in the ledger, failures included, and results are reported with the
  number of trials. Never pick the best variant out of a sensitivity or robustness report. Swings
  of about 0.1 Sharpe have come from implementation details alone: a smaller difference is not a
  finding. Every run records its universe file, code commit and settings hash; reruns use
  `--universe-file`.
- **Before every push**: `ruff check tradebot tests`, the full `pytest -q`, then `pytest -q` in a
  fresh clone of the commit. Branch `claude/loving-cannon-5lovc2` unless the owner names another.
  No model names in commits. No pull requests unless asked.

## Output rule

After each session, append an entry to **STATUS.md**, under a page: **What changed** (commits),
**What the numbers say** (with the lines that reproduce them), **What needs you**, **What's
next**. `tradebot weekly` writes the weekly one. Then work through **BACKLOG.md** from the top.
Tick items off with the commit that did them.

## Where things are

- Code: `tradebot/`. Most touched: `backtest/engine.py` (fills, candidates, flat times),
  `portfolio.py` (account replay), `reconcile.py` (paper vs backtest), `weekly.py`, `bot.py` (the
  live loop; activity log in the `botlog` table), `research.py` / `oos.py` / `protocols.py`
  (registered tests), `universe.py`, `gates/` (same-trades, deploy, drawdown).
- Scripts: `scripts/weekly.sh`, `scripts/daily.sh`, `scripts/install-cron.sh` (DEPLOY.md).
- The running bot's state: `~/tradebot/state/tradebot.db` (signals, positions, snapshots,
  `botlog`). The agent's: `~/tradebot-agent/state/research/` (the ledger, `weekly.json`,
  `drawdown-limits.json`, `drawdown-events.jsonl`, protocol files and run outputs) and
  `state/deploys.jsonl`.
- Reports: `reports/` (universe files `universe-*.json`; `reports/weekly/`; `drawdown-*.txt`).
  The weekly reference: `reports/universe-20260927-1513@ae152bd-178b23c9a0.json`.
