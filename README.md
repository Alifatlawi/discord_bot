# Rainbow Six Siege Match Bot

A Discord bot that runs **5v5 Rainbow Six Siege custom matches** end to end:

1. **`/r6 start`** — opens a lobby in the channel.
2. Players click **Join** until **10** are in.
3. The bot **randomly splits** them into two teams of 5 and picks a **leader** for each.
4. The two leaders **alternate banning maps** until **one map** remains.
5. After the game, **both leaders report the winner** — points are awarded only when they agree.
6. **`/r6 leaderboard`** tracks points, wins, and losses persistently (per server).

## Commands

| Command | What it does |
| --- | --- |
| `/r6 start` | Start a match lobby in the current channel |
| `/r6 leaderboard` | Show the top players on this server |
| `/r6 rank` | Show your own stats |
| `/r6 cancel` | Cancel the active game (starter or a `Manage Server` admin) |

## Setup

### 1. Create the bot application
1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token.
3. Leave all **Privileged Gateway Intents OFF** — they aren't needed.
4. **OAuth2 → URL Generator** → scopes: `bot` + `applications.commands`.
   Bot permissions: **Send Messages, Embed Links, Read Message History, Use Application Commands**.
   Open the generated URL to invite the bot to your server.

### 2. Configure
```bash
cp .env.example .env
# edit .env and paste your token into DISCORD_TOKEN
# (optional) set DEV_GUILD_ID to your test server's ID for instant slash-command sync
```

### 3. Install & run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

On first launch the bot creates `r6.db`, cancels any matches orphaned by a previous
run, and syncs its slash commands. With `DEV_GUILD_ID` set, commands appear instantly
in that server; otherwise global sync can take up to an hour.

## Running with Docker (recommended for a VPS)

A `Dockerfile` and `docker-compose.yml` are included. The token is read from a
`.env` file at runtime (never baked into the image), and the SQLite leaderboard
lives on a named volume so it survives restarts and rebuilds.

On the server (outside Turkey — Discord must be reachable):

```bash
git clone https://github.com/Alifatlawi/discord_bot.git
cd discord_bot
cp .env.example .env       # then edit .env and paste your DISCORD_TOKEN
docker compose up -d --build
```

Useful commands:

```bash
docker compose logs -f     # watch the bot (look for "Logged in as ...")
docker compose restart     # restart it
docker compose down        # stop it (the r6data volume / leaderboard is kept)
docker compose up -d --build   # redeploy after a git pull
```

The bot only makes outbound connections to Discord, so no ports need to be opened.

## Tuning

Everything tunable lives in **`config.py`**: scoring (`WIN_POINTS`, `LOSS_POINTS`),
timeouts (lobby fill, per-turn ban clock, result window), and `FIRST_BAN_RANDOM`.
The map pool is in **`game/maps.py`** — edit `MAP_POOL` freely (any size ≥ 2 works).

## Project layout

```
bot.py            Entrypoint: intents, setup_hook (init DB → reconcile → load cog → sync)
config.py         All tunable constants
database.py       Async SQLite layer (the only place SQL lives)
cogs/r6.py        /r6 slash commands
game/
  maps.py         MAP_POOL + slug helpers
  session.py      GameSession state machine + all interaction handling
  manager.py      One session per channel
  views/          Join / ban / result buttons
```

## Notes on robustness

- All match state is mutated under a per-session lock, so concurrent clicks and the
  phase timers can't corrupt it.
- Scoring is **idempotent** — points land at most once even under a double-click,
  guarded by a `scored 0→1` latch in a single SQLite transaction.
- Out-of-band updates (timeouts, auto-bans) edit a stored channel message, never an
  expiring interaction token.
- Live match state is in-memory: a bot restart cancels any match in progress
  (completed matches and the leaderboard persist in SQLite).
