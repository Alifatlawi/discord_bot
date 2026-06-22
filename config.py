"""Central configuration. All tunable knobs live here.

Values are read from environment variables (loaded from a local .env file) where
it makes sense, with sensible defaults for everything else.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Secrets / environment -------------------------------------------------
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

_dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
DEV_GUILD_ID: int | None = int(_dev_guild) if _dev_guild.isdigit() else None

DB_PATH: str = os.getenv("DB_PATH", "r6.db")

# --- Match rules -----------------------------------------------------------
TEAM_SIZE: int = 5
PLAYERS_NEEDED: int = TEAM_SIZE * 2  # 10

# Whoever bans first each match is chosen at random when True, else Team 1.
FIRST_BAN_RANDOM: bool = True

# --- Scoring ---------------------------------------------------------------
WIN_POINTS: int = 3
LOSS_POINTS: int = 0

# --- Timeouts (seconds) ----------------------------------------------------
# NOTE: RESULT_REPORT_TIMEOUT is intentionally < 15 min. Discord interaction
# tokens expire after 15 minutes; keeping the windows short avoids any reliance
# on a stale token. (Out-of-band edits go through stored channel Messages, which
# do not expire, but short windows keep matches snappy regardless.)
LOBBY_FILL_TIMEOUT: int = 10 * 60      # cancel the lobby if it never fills
BAN_TURN_TIMEOUT: int = 60             # per-turn clock; auto-bans a random map
RESULT_REPORT_TIMEOUT: int = 12 * 60   # cancel if leaders never agree on a winner
