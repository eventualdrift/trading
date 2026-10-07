# Moving the bot to an always-on Linux box

The bot runs in docker from one checkout (`~/tradebot`). Claude Code works in a second checkout
(`~/tradebot-agent`), reads the bot's state and price cache, and reports in STATUS.md. Code
reaches the running bot only when the owner deploys it (step 9).

Commands below assume Ubuntu 24.04 LTS and a user called `tradebot`. Replace `BOX` with the
box's address.

## 1. The box

- 2 vCPU, 2 GB RAM (learn holds a few years of candles for ~30 coins in memory), 20 GB disk.
- A region where the exchange's public API answers. Binance blocks some, e.g. the US. Check
  on the box: `curl -s https://api.binance.com/api/v3/ping` should print `{}`.

## 2. Basic hardening (once, as root or with sudo)

```bash
adduser tradebot && usermod -aG sudo tradebot
# from the Mac: ssh-copy-id tradebot@BOX, then log in as tradebot
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sshd -T | grep -Ei "passwordauthentication|permitrootlogin"   # both "no"; a file in /etc/ssh/sshd_config.d/ can override
sudo systemctl restart ssh
sudo ufw allow OpenSSH && sudo ufw enable                         # nothing else is exposed
sudo apt update && sudo apt install -y unattended-upgrades git python3-venv sqlite3 tmux
sudo dpkg-reconfigure -plow unattended-upgrades                   # security updates on their own
timedatectl | grep "synchronized: yes"                            # candle timing depends on the clock
```

Install Docker Engine from Docker's own apt repository (docs.docker.com/engine/install/ubuntu),
then `sudo usermod -aG docker tradebot` and log in again. Membership of the docker group is as
good as root: give it to this user only. No container port is published. Docker would bypass
ufw for a published port, so keep it that way.

## 3. Two checkouts

A deploy key with write access lets the agent push its work and STATUS.md:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tradebot_deploy -N ""
cat ~/.ssh/tradebot_deploy.pub   # GitHub: eventualdrift/trading > Settings > Deploy keys > Add, tick "Allow write access"
printf 'Host github-tradebot\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/tradebot_deploy\n' >> ~/.ssh/config
git clone -b claude/loving-cannon-5lovc2 github-tradebot:eventualdrift/trading.git ~/tradebot         # the running bot
git clone -b claude/loving-cannon-5lovc2 github-tradebot:eventualdrift/trading.git ~/tradebot-agent   # Claude Code works here
```

## 4. Stop the Mac's bot for good, then copy its files

Two bots must never run on the same paper account or Telegram token. On the Mac:

```bash
launchctl bootout gui/$(id -u)/com.janulouw.tradebot     # stops it (a clean stop: it is recorded)
launchctl disable gui/$(id -u)/com.janulouw.tradebot     # and keeps it from starting at the next login
cd ~/tradebot && rsync -av state data reports config.yaml .env tradebot@BOX:tradebot/
```

`state/` holds the database (signals, positions, the activity log), the selected strategies and
the research ledger (`state/research/`; it moves to the agent's checkout in step 6). `data/` holds the price cache and the out-of-sample
data. `reports/` holds the universe files. Then on the box:

```bash
chmod 600 ~/tradebot/.env ~/tradebot/config.yaml
grep -n "EXCHANGE_API" ~/tradebot/.env   # paper needs no exchange keys: delete any such lines
```

## 5. Start the bot

```bash
cd ~/tradebot && TRADEBOT_UID=$(id -u) TRADEBOT_GID=$(id -g) GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose logs -f     # "tradebot running in PAPER mode"; Telegram says "tradebot started"
```

It restarts by itself after a crash or a reboot (`restart: unless-stopped`), and logs rotate at
5 x 10 MB. The dashboard is still written to `state/dashboard.html`. To view it from the Mac:
`scp tradebot@BOX:tradebot/state/dashboard.html /tmp/ && open /tmp/dashboard.html`.

## 6. The agent checkout

The agent writes only to its own copies. It reads the bot's database read-only and the bot's
strategies read-only (CLAUDE.md, hard rules):

```bash
cd ~/tradebot-agent && python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]" && .venv/bin/pytest -q
mkdir -p state && rsync -a ~/tradebot/data/ data/ && rsync -a ~/tradebot/reports/ reports/   # its own copies
mv ~/tradebot/state/research state/research   # the research ledger moves here: research commands run from this checkout
cat > config-agent.yaml <<END
extends: $HOME/tradebot/config.yaml
state_dir: $HOME/tradebot-agent/state        # the agent's own: research ledger, weekly state
data:
  dir: $HOME/tradebot-agent/data             # the agent's own candle cache: unfrozen runs write here
observe_state_dir: $HOME/tradebot/state      # the running bot's database, opened read-only
learning:
  follow_state_dir: $HOME/tradebot/state     # the bot's strategies, read-only; learn refuses with this config
END
git config user.name "tradebot agent" && git config user.email "you@example.com"
.venv/bin/tradebot --config config-agent.yaml report     # the running bot's track record, read from its database
```

`config-agent.yaml` is git-ignored. With it, `run` and `learn` refuse to start, and the bot's
database can't be written: it is opened read-only, and its tables are never created or migrated
from here. Frozen runs read the agent's own candle copy, and fetch (without saving) anything
newer than it.

## 7. Claude Code with Remote Control

Install Claude Code on the box (official instructions) and sign in once. Then, in a tmux
session so it survives disconnects:

```bash
tmux new -s agent
cd ~/tradebot-agent && claude remote-control
# Ctrl-b d to detach; tmux attach -t agent to come back
```

The session shows up in the Claude Code app, and you can work with it from there. It reads
CLAUDE.md for its rules and BACKLOG.md for its work, and writes STATUS.md.

## 8. The weekly check-in

Set the reference backtest once (the stamped universe file with the full selection; see the
first item of BACKLOG.md):

```bash
cd ~/tradebot-agent && .venv/bin/tradebot --config config-agent.yaml weekly --reference reports/universe-20260927-1513@ae152bd-178b23c9a0.json
```

Then `crontab -e` and add the weekly run, which pushes STATUS.md, and a nightly backup:

```
15 6 * * 1 cd $HOME/tradebot-agent && .venv/bin/tradebot --config config-agent.yaml weekly >> $HOME/weekly.log 2>&1 && git add STATUS.md && git commit -qm "Weekly check-in $(date -u +\%F)" && git pull -q --rebase --autostash && git push -q
30 3 * * * mkdir -p $HOME/backups && sqlite3 $HOME/tradebot/state/tradebot.db ".backup '$HOME/backups/tradebot-$(date -u +\%F).db'" && tar -czf $HOME/backups/research-$(date -u +\%F).tgz -C $HOME/tradebot-agent/state research && find $HOME/backups -mtime +14 -delete
```

Every Monday, STATUS.md on GitHub gets the week: uptime, paper vs backtest, the frozen
reference rerun, what needs you. Copy the backups off the box now and then, e.g.
`rsync -av tradebot@BOX:backups ~/tradebot-backups` from the Mac.

## 9. Deploying a new version (only when the owner says so)

```bash
cd ~/tradebot && git pull && TRADEBOT_UID=$(id -u) TRADEBOT_GID=$(id -g) GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

Docker sends SIGTERM. The bot finishes its tick and records a clean stop; the new container
records the start. The tests ran before the push (CLAUDE.md), so this is the only step.

## 10. After the move

- `docker compose ps` shows the container up. A day later the agent's
  `reconcile --since "<move time>"` shows the Mac's stop, the box's start, and uptime near 100%.
- `docker compose logs --since 24h | grep -E "tick failed|skipping stale"` should print nothing.
- Keep the Mac's bot disabled. If the Mac is used again, use it for reports only.
