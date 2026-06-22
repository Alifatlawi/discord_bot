"""Async SQLite persistence layer.

Owns the single aiosqlite connection. ALL SQL lives here — nothing else in the
codebase touches the database directly.

The connection is opened with ``isolation_level=None`` (autocommit) so that:
  * simple single-statement writes commit immediately, and
  * the scoring path can use an explicit ``BEGIN IMMEDIATE`` ... ``COMMIT`` for a
    truly atomic, idempotent points commit.
"""

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_stats (
  guild_id        INTEGER NOT NULL,
  discord_user_id INTEGER NOT NULL,
  display_name    TEXT    NOT NULL,
  points          INTEGER NOT NULL DEFAULT 0,
  wins            INTEGER NOT NULL DEFAULT 0,
  losses          INTEGER NOT NULL DEFAULT 0,
  matches_played  INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (guild_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS matches (
  match_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id        INTEGER NOT NULL,
  channel_id      INTEGER,
  status          TEXT    NOT NULL DEFAULT 'banning'
                  CHECK(status IN ('banning','awaiting_result','completed','cancelled')),
  team1_leader_id INTEGER,
  team2_leader_id INTEGER,
  map_played      TEXT,
  winner_team     INTEGER,
  scored          INTEGER NOT NULL DEFAULT 0 CHECK(scored IN (0,1)),
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS match_participants (
  match_id        INTEGER NOT NULL,
  discord_user_id INTEGER NOT NULL,
  team            INTEGER NOT NULL CHECK(team IN (1,2)),
  is_leader       INTEGER NOT NULL DEFAULT 0 CHECK(is_leader IN (0,1)),
  PRIMARY KEY (match_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS match_results (
  match_id             INTEGER NOT NULL,
  reporting_leader_id  INTEGER NOT NULL,
  reported_winner_team INTEGER NOT NULL CHECK(reported_winner_team IN (1,2)),
  reported_at          TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (match_id, reporting_leader_id)
);

CREATE TABLE IF NOT EXISTS map_bans (
  match_id       INTEGER NOT NULL,
  ban_order      INTEGER NOT NULL,
  banned_by_team INTEGER NOT NULL CHECK(banned_by_team IN (1,2)),
  map_name       TEXT    NOT NULL,
  PRIMARY KEY (match_id, ban_order)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()

    async def reconcile_orphans(self) -> int:
        """Cancel any matches left unfinished by a previous crash/restart.

        In-memory session state does not survive a restart, so anything still in
        a live state is dead. Returns the number of matches cancelled.
        """
        cur = await self.conn.execute(
            "UPDATE matches SET status='cancelled' "
            "WHERE status IN ('banning','awaiting_result') AND scored=0"
        )
        return cur.rowcount

    # --- players -----------------------------------------------------------
    async def upsert_player(self, guild_id: int, user_id: int, name: str) -> None:
        await self.conn.execute(
            "INSERT INTO player_stats (guild_id, discord_user_id, display_name) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, discord_user_id) "
            "DO UPDATE SET display_name=excluded.display_name",
            (guild_id, user_id, name),
        )

    async def top_players(self, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT display_name, points, wins, losses, matches_played "
            "FROM player_stats WHERE guild_id=? AND matches_played>0 "
            "ORDER BY points DESC, wins DESC, losses ASC, display_name ASC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def get_player(self, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT display_name, points, wins, losses, matches_played "
            "FROM player_stats WHERE guild_id=? AND discord_user_id=?",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    # --- matches -----------------------------------------------------------
    async def create_match(
        self, guild_id: int, channel_id: int, leader1: int, leader2: int
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO matches (guild_id, channel_id, status, team1_leader_id, team2_leader_id) "
            "VALUES (?, ?, 'banning', ?, ?)",
            (guild_id, channel_id, leader1, leader2),
        )
        return cur.lastrowid

    async def add_participants(self, rows: list[tuple[int, int, int, int]]) -> None:
        """rows = [(match_id, user_id, team, is_leader), ...]"""
        await self.conn.executemany(
            "INSERT OR REPLACE INTO match_participants "
            "(match_id, discord_user_id, team, is_leader) VALUES (?, ?, ?, ?)",
            rows,
        )

    async def log_ban(
        self, match_id: int, ban_order: int, team: int, map_name: str
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO map_bans (match_id, ban_order, banned_by_team, map_name) "
            "VALUES (?, ?, ?, ?)",
            (match_id, ban_order, team, map_name),
        )

    async def set_map_played(self, match_id: int, map_name: str) -> None:
        await self.conn.execute(
            "UPDATE matches SET map_played=?, status='awaiting_result' WHERE match_id=?",
            (map_name, match_id),
        )

    async def cancel_match(self, match_id: int) -> None:
        await self.conn.execute(
            "UPDATE matches SET status='cancelled' WHERE match_id=? AND scored=0",
            (match_id,),
        )

    # --- results & scoring -------------------------------------------------
    async def upsert_result(self, match_id: int, leader_id: int, team: int) -> None:
        await self.conn.execute(
            "INSERT INTO match_results (match_id, reporting_leader_id, reported_winner_team) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(match_id, reporting_leader_id) "
            "DO UPDATE SET reported_winner_team=excluded.reported_winner_team, "
            "reported_at=datetime('now')",
            (match_id, leader_id, team),
        )

    async def commit_score(self, match_id: int, winner_team: int) -> bool:
        """Atomically award points for ``match_id`` exactly once.

        Returns True if THIS call performed the scoring, False if the match was
        already scored (the idempotency latch). Safe under double-clicks/races.
        """
        conn = self.conn
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "UPDATE matches SET scored=1, status='completed', winner_team=?, "
                "completed_at=datetime('now') WHERE match_id=? AND scored=0",
                (winner_team, match_id),
            )
            if cur.rowcount == 0:
                await conn.execute("ROLLBACK")
                return False

            grow = await (
                await conn.execute(
                    "SELECT guild_id FROM matches WHERE match_id=?", (match_id,)
                )
            ).fetchone()
            guild_id = grow["guild_id"]

            participants = await (
                await conn.execute(
                    "SELECT discord_user_id, team FROM match_participants WHERE match_id=?",
                    (match_id,),
                )
            ).fetchall()

            for p in participants:
                won = p["team"] == winner_team
                await conn.execute(
                    "UPDATE player_stats SET "
                    "points=points+?, wins=wins+?, losses=losses+?, "
                    "matches_played=matches_played+1, updated_at=datetime('now') "
                    "WHERE guild_id=? AND discord_user_id=?",
                    (
                        config.WIN_POINTS if won else config.LOSS_POINTS,
                        1 if won else 0,
                        0 if won else 1,
                        guild_id,
                        p["discord_user_id"],
                    ),
                )
            await conn.execute("COMMIT")
            return True
        except Exception:
            await conn.execute("ROLLBACK")
            raise
