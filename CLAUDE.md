# Working on tradebot

A crypto trading bot that is **paper trading**: a 65/35 core/satellite account (BTC/ETH trend
core, signal-strategy satellite) with a go-live checklist. The owner decides; you build, measure
and report. When in doubt, stop and ask (below).

## Hard rules: never, without the owner's explicit OK for that specific action

- **Paper only.** Never set `mode: live`, never add, use or ask for exchange API keys, never
  pass `--force-live`.
- **Never change what live trades or what learn selects** without a registered test and the
  owner's OK. That covers strategies and their parameters and filters, entry/exit rules, risk
  and sizing, the coin universe rule, the core's rules and split, costs used by selection,
  learn's selection criteria, and any setting in `config.yaml` (`tradebot config-set` included).
- **Deploy to the running bot only when the owner says so.** Don't `git pull`, rebuild or
  restart in the bot's checkout or container. Work in the agent checkout.
- Never run `tradebot run` or `tradebot learn` against the running bot's state. Never commit
  `.env`, `config*.yaml`, `state/`, `data/` or `reports/`.

## What you may do unasked

- Reporting and analysis: `tradebot weekly`, `reconcile`, frozen `portfolio-backtest` reruns,
  `research core` (a robustness report: it never selects a variant).
- **Fidelity fixes**: make the backtest model what live actually does, found by the
  reconciliation or by reading the code. Each fix is followed by a frozen rerun on the
  reference universe file and a ledger note (`tradebot research note --id ... --text ...`) on
  every recorded result it affects. Recorded verdicts are never edited or rerun.
- Tests, logging, docs, and bug fixes that don't change trades.

A change doesn't change trades when the live path (scanner, risk checks, entries, exits, core
rebalance) makes the same orders on the same data, and learn makes the same selection. If you
can't show that, it's a stop-and-ask.

## Research discipline

- New rules are judged on **untouched data**: data not used to design or tune them.
- A **registered protocol** (hypothesis, data, metric, pass rule, how each outcome is read) is
  in the ledger before any run that produces a verdict. You draft it; the owner registers it.
- **One run per protocol.** A second look needs a new protocol id and counts as another trial.
- **Registered-test steps go in separate command blocks**, each reviewed before the next.
- Every run records its **universe file** (coins, data end, full strategy selection), **code
  commit** and **settings hash**. Reruns use `--universe-file`.
- **Trials are counted**: every variant tried goes in the ledger, failures included, and results
  are reported with the number of trials.
- Never pick the best variant out of a sensitivity or robustness report.

## Stop and ask

Write the item under "What needs you" in STATUS.md, stop that thread, and carry on with
anything else on the backlog:
- anything under the hard rules;
- a protocol to register;
- a result that changes a conclusion (for example, a frozen rerun moves a recorded finding);
- a go-live item (see `tradebot report`: the checklist, and the backlog's go-live section);
- anything that might be tuning: choosing strategies, parameters, thresholds or splits because
  of results.

## Output rule

After each session, append an entry to **STATUS.md**, under a page:
**What changed** (commits), **What the numbers say** (with the lines that reproduce them),
**What needs you**, **What's next**. `tradebot weekly` writes the weekly one; write your own
for work sessions. Then work through **BACKLOG.md** from the top, and stop at the stop-and-ask
list. Tick items off with the commit that did them.

## Before every push

`ruff check` on the files you changed, the full `pytest -q`, then `pytest -q` in a fresh clone
of the commit. Push only when all three pass. Don't put model names in commits. Branch:
`claude/loving-cannon-5lovc2` unless the owner names another. Don't open pull requests unless asked.

## Where things are

- Code: `tradebot/`. The ones you'll touch most: `backtest/engine.py` (fills, candidates,
  flat times), `portfolio.py` (account replay, whole-account backtest), `reconcile.py` (paper vs
  backtest), `weekly.py`, `bot.py` (the live loop; its activity log goes in the `botlog` table),
  `research.py` / `oos.py` (registered tests), `universe.py` (universe files).
- The running bot's state: `state/tradebot.db` (signals, positions, snapshots, `botlog`),
  `state/research/ledger.jsonl` (registered tests, results and notes; append-only),
  `state/research/weekly.json` (the weekly reference and last week's numbers).
- Reports: `reports/` (universe files `universe-*.json`; weekly reports in `reports/weekly/`).
- On the always-on box (DEPLOY.md), the bot runs from `~/tradebot` in docker. You work in
  `~/tradebot-agent` with `--config config-agent.yaml`, which reads the bot's state and price
  cache.
