"""SessionManager: one live GameSession per channel.

The registry is in-memory only — live match state does not survive a restart
(persistent data lives in SQLite). A single instance is held on the bot.
"""

from __future__ import annotations

from game.session import GameSession


class SessionManager:
    def __init__(self):
        self._sessions: dict[int, GameSession] = {}

    def get(self, channel_id: int) -> GameSession | None:
        return self._sessions.get(channel_id)

    def create(
        self,
        bot,
        guild_id: int,
        channel_id: int,
        channel,
        starter_id: int,
        starter_name: str,
    ) -> GameSession:
        session = GameSession(
            bot, self, guild_id, channel_id, channel, starter_id, starter_name
        )
        self._sessions[channel_id] = session
        return session

    def remove(self, channel_id: int) -> None:
        self._sessions.pop(channel_id, None)
