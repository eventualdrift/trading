#!/usr/bin/env bash
# Every day: gate 3, the drawdown check (installed by scripts/install-cron.sh). On a hit it
# reconciles the period and checks the range against the backtest, records the outcome in
# state/research/drawdown-events.jsonl and exits 10 (in range: recorded, nothing to do),
# 11 (outside the range with correct mechanics: a registered test) or 12 (mechanics differ: fix
# them). 11 and 12 are sent to Telegram (--notify): the agent session picks them up.
set -u
cd "$(dirname "$0")/.." || exit 1
TB=(.venv/bin/tradebot --config config-agent.yaml)
mkdir -p reports
log="reports/drawdown-cron.log"
date -u +"== %F %T UTC" >>"$log"
"${TB[@]}" gate drawdown --notify >>"$log" 2>&1
code=$?
case $code in
    0|10|11|12) ;;  # no hit, or a hit the gate recorded (and reported, for 11 and 12)
    *) "${TB[@]}" notify --text "Daily drawdown gate on $(hostname) failed (exit $code): $(tail -n 5 "$log")" \
           >>"$log" 2>&1 ;;
esac
exit 0
