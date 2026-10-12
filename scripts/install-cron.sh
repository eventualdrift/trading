#!/usr/bin/env bash
# Install the agent's cron jobs, replacing earlier ones (run it again after moving a checkout):
#   Monday 06:17   scripts/weekly.sh  - weekly check-in, Telegram summary, STATUS.md pushed
#   daily  06:47   scripts/daily.sh   - the drawdown gate
#   daily  03:31   scripts/backup.sh  - the bot's database and the research folder (14 days kept)
# Times are the box's clock (timedatectl); any time on Monday works for the weekly.
# Usage: scripts/install-cron.sh [bot checkout, default ~/tradebot]
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
bot="${1:-$HOME/tradebot}"
tag="# tradebot-agent"
[ -x "$here/.venv/bin/tradebot" ] || { echo "no venv in $here: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"; exit 1; }
[ -f "$here/.env" ] || echo "note: $here/.env has no Telegram lines yet; the jobs can't message the owner until it does"
{
    crontab -l 2>/dev/null | grep -v -- "$tag" || true
    echo "17 6 * * 1 $here/scripts/weekly.sh $tag"
    echo "47 6 * * * $here/scripts/daily.sh $tag"
    echo "31 3 * * * $here/scripts/backup.sh $bot $tag"
} | crontab -
crontab -l | grep -- "$tag"
