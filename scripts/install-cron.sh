#!/usr/bin/env bash
# Install the agent's cron jobs, replacing earlier ones (run it again after moving a checkout):
#   Monday 06:17   scripts/weekly.sh  - weekly check-in, Telegram summary, STATUS.md pushed
#   daily  06:47   scripts/daily.sh   - the drawdown gate
#   daily  03:31   backup of the bot's database and the research ledger (14 days kept)
# Times are the box's clock (timedatectl); any time on Monday works for the weekly.
# Usage: scripts/install-cron.sh [bot checkout, default ~/tradebot]
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
bot="${1:-$HOME/tradebot}"
tag="# tradebot-agent"
backup="mkdir -p \$HOME/backups && sqlite3 $bot/state/tradebot.db \".backup '\$HOME/backups/tradebot-\$(date -u +\\%F).db'\""
backup="$backup && tar -czf \$HOME/backups/research-\$(date -u +\\%F).tgz -C $here/state research"
backup="$backup && find \$HOME/backups -mtime +14 -delete"
{
    crontab -l 2>/dev/null | grep -v -- "$tag" || true
    echo "17 6 * * 1 $here/scripts/weekly.sh $tag"
    echo "47 6 * * * $here/scripts/daily.sh $tag"
    echo "31 3 * * * $backup $tag"
} | crontab -
crontab -l | grep -- "$tag"
