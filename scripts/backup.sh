#!/usr/bin/env bash
# Nightly backup (installed by scripts/install-cron.sh): the bot's database through sqlite's online
# backup from a read-only connection (the venv's python; no sqlite3 binary needed), and the agent's
# research folder (ledger, limits, events). 14 days are kept. A failure is sent to the owner.
# Usage: scripts/backup.sh [bot checkout, default ~/tradebot]
set -u
cd "$(dirname "$0")/.." || exit 1
bot="${1:-$HOME/tradebot}"
out="$HOME/backups"
mkdir -p "$out"
stamp=$(date -u +%F)
if ! .venv/bin/python -c '
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close()
dst.close()
' "$bot/state/tradebot.db" "$out/tradebot-$stamp.db"; then
    .venv/bin/tradebot --config config-agent.yaml notify --text "Nightly backup on $(hostname) failed: the database"
    exit 1
fi
if ! tar -czf "$out/research-$stamp.tgz" -C state research; then
    .venv/bin/tradebot --config config-agent.yaml notify --text "Nightly backup on $(hostname) failed: state/research"
    exit 1
fi
find "$out" -mtime +14 -delete
