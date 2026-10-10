#!/usr/bin/env bash
# Monday's weekly check-in on the always-on box (installed by scripts/install-cron.sh): the weekly
# report appended to STATUS.md, the Telegram summary, STATUS.md committed and pushed. A step that
# fails is sent to the owner with `tradebot notify` (CLAUDE.md, gate 4).
set -u
cd "$(dirname "$0")/.." || exit 1
TB=(.venv/bin/tradebot --config config-agent.yaml)
mkdir -p reports/weekly
log="reports/weekly/cron-$(date -u +%F).log"

fail() {
    "${TB[@]}" notify --text "Weekly cron on $(hostname): $1. Log: $PWD/$log" >>"$log" 2>&1
    exit 1
}

"${TB[@]}" weekly --notify >>"$log" 2>&1 || fail "tradebot weekly failed: $(tail -n 5 "$log")"
git commit -q -m "Weekly check-in $(date -u +%F)" -- STATUS.md >>"$log" 2>&1 || fail "could not commit STATUS.md"
git pull -q --rebase --autostash >>"$log" 2>&1 || fail "git pull failed (STATUS.md is committed locally)"
git push -q >>"$log" 2>&1 || fail "git push failed (STATUS.md is committed locally)"
