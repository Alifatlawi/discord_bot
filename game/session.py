"""GameSession: the in-memory state machine for a single match in a channel.

Design rules (from the reviewed plan, §8 — these are load-bearing):
  * Every mutation runs under ``self.lock`` so concurrent button clicks and timer
    callbacks can never interleave mid-update.
  * All OUT-OF-BAND edits (timer callbacks, auto-bans, transitions) go through the
    stored ``self.message`` (a regular channel Message), never a stale interaction
    token. The lobby message is the only interaction-response message and is only
    touched within its 15-minute token life.
  * Phase clocks are driven by asyncio tasks, not View timeouts (Views are
    ``timeout=None`` so their buttons never silently die).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random

import discord

import config
from game import maps

log = logging.getLogger("r6bot.session")


class GameState(enum.Enum):
    RECRUITING = "recruiting"
    MAP_BAN = "map_ban"
    IN_PROGRESS = "in_progress"
    RESULT_PENDING = "result_pending"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


TERMINAL = {GameState.COMPLETE, GameState.CANCELLED}


class GameSession:
    def __init__(
        self,
        bot,
        manager,
        guild_id: int,
        channel_id: int,
        channel: discord.abc.Messageable,
        starter_id: int,
        starter_name: str,
    ):
        self.bot = bot
        self.db = bot.db
        self.manager = manager
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.channel = channel
        self.starter_id = starter_id

        self.state = GameState.RECRUITING
        self.lock = asyncio.Lock()
        self.message: discord.Message | None = None
        self._timer: asyncio.Task | None = None

        # roster: insertion-ordered dict used as an ordered set of user ids
        self.players: dict[int, bool] = {}
        self.display_names: dict[int, str] = {starter_id: starter_name}

        # set once teams are formed
        self.team1: list[int] = []
        self.team2: list[int] = []
        self.leader1: int | None = None
        self.leader2: int | None = None
        self.ban_order: list[int] = []

        # ban phase
        self.remaining: list[str] = []
        self.banned: list[tuple[str, int]] = []  # (map_name, team)

        # result phase
        self.match_id: int | None = None
        self.map_played: str | None = None
        self.reports: dict[int, int] = {}  # leader_id -> team voted
        self.winner_team: int | None = None

    # ===================================================================== #
    # Small helpers
    # ===================================================================== #
    def name(self, uid: int) -> str:
        return self.display_names.get(uid, f"User {uid}")

    def team_of(self, uid: int) -> int | None:
        if uid in self.team1:
            return 1
        if uid in self.team2:
            return 2
        return None

    def current_leader(self) -> int:
        return self.ban_order[len(self.banned) % 2]

    def is_leader(self, uid: int) -> bool:
        return uid in (self.leader1, self.leader2)

    def form_teams(self) -> None:
        roster = list(self.players)
        random.shuffle(roster)
        self.team1 = roster[: config.TEAM_SIZE]
        self.team2 = roster[config.TEAM_SIZE : config.PLAYERS_NEEDED]
        self.leader1 = self.team1[0]
        self.leader2 = self.team2[0]
        self.remaining = list(maps.MAP_POOL)
        self.banned = []
        if config.FIRST_BAN_RANDOM:
            first = random.choice([self.leader1, self.leader2])
        else:
            first = self.leader1
        second = self.leader2 if first == self.leader1 else self.leader1
        self.ban_order = [first, second]

    def _apply_ban(self, map_name: str) -> tuple[int, int]:
        team = self.team_of(self.current_leader())
        order = len(self.banned) + 1
        self.banned.append((map_name, team))
        self.remaining.remove(map_name)
        return order, team

    # ===================================================================== #
    # Timer plumbing (single active phase clock)
    # ===================================================================== #
    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _arm_timer(self, seconds: int, callback) -> None:
        self._cancel_timer()
        self._timer = asyncio.create_task(self._timer_runner(seconds, callback))

    async def _timer_runner(self, seconds: int, callback) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        try:
            await callback()
        except Exception:
            log.exception("timer callback failed for channel %s", self.channel_id)

    # ===================================================================== #
    # Lobby
    # ===================================================================== #
    async def begin_lobby(self, message: discord.Message) -> None:
        self.message = message
        self._arm_timer(config.LOBBY_FILL_TIMEOUT, self._on_lobby_timeout)

    async def handle_join(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.state != GameState.RECRUITING:
                await interaction.response.send_message(
                    "This lobby is no longer open.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid in self.players:
                await interaction.response.send_message(
                    "You're already in the lobby.", ephemeral=True
                )
                return

            self.players[uid] = True
            self.display_names[uid] = interaction.user.display_name
            await self.db.upsert_player(
                self.guild_id, uid, interaction.user.display_name
            )

            if len(self.players) >= config.PLAYERS_NEEDED:
                # This joiner's own (fresh) interaction closes the lobby message.
                await interaction.response.edit_message(
                    embed=self._lobby_full_embed(), view=None
                )
                await self._start_match()
            else:
                await interaction.response.edit_message(
                    embed=self._lobby_embed(), view=self._lobby_view()
                )

    async def handle_leave(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.state != GameState.RECRUITING:
                await interaction.response.send_message(
                    "The lobby is closed.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid not in self.players:
                await interaction.response.send_message(
                    "You're not in the lobby.", ephemeral=True
                )
                return
            self.players.pop(uid, None)
            await interaction.response.edit_message(
                embed=self._lobby_embed(), view=self._lobby_view()
            )

    async def handle_cancel_button(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.state in TERMINAL:
                await interaction.response.send_message(
                    "This game is already over.", ephemeral=True
                )
                return
            if not self._may_cancel(interaction.user):
                await interaction.response.send_message(
                    "Only the game starter or a server admin can cancel.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            await self._do_cancel(f"Cancelled by {interaction.user.display_name}.")

    async def _on_lobby_timeout(self) -> None:
        async with self.lock:
            if self.state != GameState.RECRUITING:
                return
            await self._do_cancel("Not enough players joined in time.")

    # ===================================================================== #
    # Match start -> ban phase
    # ===================================================================== #
    async def _start_match(self) -> None:
        self._cancel_timer()  # stop the lobby-fill clock
        self.state = GameState.MAP_BAN
        self.form_teams()
        self.match_id = await self.db.create_match(
            self.guild_id, self.channel_id, self.leader1, self.leader2
        )
        rows = [
            (self.match_id, u, 1, 1 if u == self.leader1 else 0) for u in self.team1
        ] + [
            (self.match_id, u, 2, 1 if u == self.leader2 else 0) for u in self.team2
        ]
        await self.db.add_participants(rows)

        # Fresh regular channel message — editable for the whole ban+result phases
        # (no expiring interaction token involved).
        self.message = await self.channel.send(
            embed=self._ban_embed(), view=self._ban_view()
        )
        self._arm_timer(config.BAN_TURN_TIMEOUT, self._on_ban_timeout)

    async def handle_ban(self, interaction: discord.Interaction, map_name: str) -> None:
        async with self.lock:
            if self.state != GameState.MAP_BAN:
                await interaction.response.send_message(
                    "The ban phase is over.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid != self.current_leader():
                msg = (
                    "It's not your turn to ban yet."
                    if self.is_leader(uid)
                    else "Only team leaders can ban maps."
                )
                await interaction.response.send_message(msg, ephemeral=True)
                return
            if map_name not in self.remaining:
                await interaction.response.send_message(
                    "That map is already banned.", ephemeral=True
                )
                return

            order, team = self._apply_ban(map_name)
            await self.db.log_ban(self.match_id, order, team, map_name)

            if len(self.remaining) == 1:
                await interaction.response.defer()
                await self._finish_ban()
            else:
                self._arm_timer(config.BAN_TURN_TIMEOUT, self._on_ban_timeout)
                await interaction.response.edit_message(
                    embed=self._ban_embed(), view=self._ban_view()
                )

    async def _on_ban_timeout(self) -> None:
        async with self.lock:
            if self.state != GameState.MAP_BAN:
                return
            map_name = random.choice(self.remaining)
            order, team = self._apply_ban(map_name)
            await self.db.log_ban(self.match_id, order, team, map_name)
            if len(self.remaining) == 1:
                await self._finish_ban()
            else:
                self._arm_timer(config.BAN_TURN_TIMEOUT, self._on_ban_timeout)
                await self.message.edit(
                    embed=self._ban_embed(auto_banned=map_name), view=self._ban_view()
                )

    async def _finish_ban(self) -> None:
        self._cancel_timer()
        self.map_played = self.remaining[0]
        self.state = GameState.IN_PROGRESS
        await self.db.set_map_played(self.match_id, self.map_played)

        # Finalize the ban message as a record, then post a fresh result message
        # that pings both leaders.
        await self.message.edit(embed=self._map_selected_embed(), view=None)
        self.message = await self.channel.send(
            content=f"<@{self.leader1}> <@{self.leader2}> — report the winner below.",
            embed=self._result_embed(),
            view=self._result_view(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        self._arm_timer(config.RESULT_REPORT_TIMEOUT, self._on_result_timeout)

    # ===================================================================== #
    # Result reporting
    # ===================================================================== #
    async def handle_report(self, interaction: discord.Interaction, team: int) -> None:
        async with self.lock:
            if self.state not in (GameState.IN_PROGRESS, GameState.RESULT_PENDING):
                await interaction.response.send_message(
                    "This match is no longer accepting results.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if not self.is_leader(uid):
                await interaction.response.send_message(
                    "Only team leaders can report the result.", ephemeral=True
                )
                return

            self.reports[uid] = team
            await self.db.upsert_result(self.match_id, uid, team)

            agreed = (
                len(self.reports) == 2 and len(set(self.reports.values())) == 1
            )
            if agreed:
                await interaction.response.defer()
                await self._commit(team)
            else:
                self.state = GameState.RESULT_PENDING
                await interaction.response.edit_message(
                    embed=self._result_embed(), view=self._result_view()
                )

    async def _commit(self, winner: int) -> None:
        self._cancel_timer()
        scored = await self.db.commit_score(self.match_id, winner)
        self.state = GameState.COMPLETE
        self.winner_team = winner
        await self.message.edit(embed=self._final_embed(scored), view=None)
        self.manager.remove(self.channel_id)

    async def _on_result_timeout(self) -> None:
        async with self.lock:
            if self.state not in (GameState.IN_PROGRESS, GameState.RESULT_PENDING):
                return
            await self._do_cancel(
                "Leaders did not agree on a result in time. No points awarded."
            )

    # ===================================================================== #
    # Cancellation (shared by button, slash command, and timeouts)
    # ===================================================================== #
    def _may_cancel(self, user: discord.Member) -> bool:
        perms = getattr(user, "guild_permissions", None)
        return user.id == self.starter_id or bool(perms and perms.manage_guild)

    async def cancel_external(self, reason: str) -> bool:
        """Cancel from outside a button (e.g. the /r6 cancel slash command)."""
        async with self.lock:
            if self.state in TERMINAL:
                return False
            await self._do_cancel(reason)
            return True

    async def _do_cancel(self, reason: str) -> None:
        """Must be called while holding ``self.lock``."""
        self._cancel_timer()
        self.state = GameState.CANCELLED
        if self.match_id is not None:
            await self.db.cancel_match(self.match_id)
        if self.message is not None:
            try:
                await self.message.edit(
                    content=None, embed=self._cancelled_embed(reason), view=None
                )
            except discord.HTTPException:
                log.warning("could not edit message on cancel for %s", self.channel_id)
        self.manager.remove(self.channel_id)

    # ===================================================================== #
    # View builders (rebuilt from state each render)
    # ===================================================================== #
    def _lobby_view(self):
        from game.views.lobby import LobbyView

        return LobbyView(self)

    def _ban_view(self):
        from game.views.ban import BanView

        return BanView(self)

    def _result_view(self):
        from game.views.result import ResultView

        return ResultView(self)

    # ===================================================================== #
    # Embeds
    # ===================================================================== #
    def _fmt_team(self, team: list[int], leader: int) -> str:
        return "\n".join(
            ("👑 " if u == leader else "• ") + self.name(u) for u in team
        )

    def _lobby_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🎮 Rainbow Six Siege — Custom Match",
            description=(
                "Click **Join** to enter the lobby. The match starts automatically "
                f"once **{config.PLAYERS_NEEDED} players** are in."
            ),
            color=discord.Color.blurple(),
        )
        roster = (
            "\n".join(f"{i + 1}. {self.name(u)}" for i, u in enumerate(self.players))
            or "_No players yet — be the first!_"
        )
        e.add_field(
            name=f"Players ({len(self.players)}/{config.PLAYERS_NEEDED})",
            value=roster,
            inline=False,
        )
        e.set_footer(
            text=(
                f"Started by {self.name(self.starter_id)} • "
                f"expires in {config.LOBBY_FILL_TIMEOUT // 60} min if not filled"
            )
        )
        return e

    def _lobby_full_embed(self) -> discord.Embed:
        return discord.Embed(
            title="✅ Lobby Full!",
            description="Shuffling teams and selecting leaders…",
            color=discord.Color.green(),
        )

    def _ban_embed(self, auto_banned: str | None = None) -> discord.Embed:
        cl = self.current_leader()
        ct = self.team_of(cl)
        total = len(maps.MAP_POOL) - 1
        e = discord.Embed(
            title="🗺️ Map Ban Phase",
            description="Leaders take turns banning maps until **one** remains.",
            color=discord.Color.orange(),
        )
        e.add_field(name="🔵 Team 1", value=self._fmt_team(self.team1, self.leader1), inline=True)
        e.add_field(name="🔴 Team 2", value=self._fmt_team(self.team2, self.leader2), inline=True)
        e.add_field(
            name="⏱️ On the clock",
            value=(
                f"**{self.name(cl)}** (Team {ct}) — pick a map to ban "
                f"(auto-ban in {config.BAN_TURN_TIMEOUT}s)"
            ),
            inline=False,
        )
        if self.banned:
            e.add_field(
                name=f"Banned ({len(self.banned)}/{total})",
                value="\n".join(f"~~{m}~~ · Team {t}" for m, t in self.banned),
                inline=False,
            )
        if auto_banned:
            e.set_footer(text=f"⏰ {auto_banned} was auto-banned (turn timed out).")
        return e

    def _map_selected_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"🗺️ Map Selected: {self.map_played}",
            description="Ban phase complete. Play the match, then report the result below.",
            color=discord.Color.green(),
        )
        e.add_field(name="🔵 Team 1", value=self._fmt_team(self.team1, self.leader1), inline=True)
        e.add_field(name="🔴 Team 2", value=self._fmt_team(self.team2, self.leader2), inline=True)
        return e

    def _result_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🏆 Report the Winner",
            description=(
                f"Map: **{self.map_played}**\n"
                "Both team leaders must report the **same** winner to lock it in."
            ),
            color=discord.Color.gold(),
        )

        def vote(uid: int) -> str:
            t = self.reports.get(uid)
            return f"✅ voted **Team {t}**" if t else "⏳ not yet voted"

        e.add_field(
            name="🔵 Team 1 leader",
            value=f"{self.name(self.leader1)} — {vote(self.leader1)}",
            inline=False,
        )
        e.add_field(
            name="🔴 Team 2 leader",
            value=f"{self.name(self.leader2)} — {vote(self.leader2)}",
            inline=False,
        )
        if len(self.reports) == 2 and len(set(self.reports.values())) == 2:
            e.add_field(
                name="⚠️ Disagreement",
                value="Leaders reported different winners. Click again to change your vote.",
                inline=False,
            )
        e.set_footer(text=f"Result window: {config.RESULT_REPORT_TIMEOUT // 60} min")
        return e

    def _final_embed(self, scored: bool) -> discord.Embed:
        winner = self.winner_team
        wteam, wleader = (
            (self.team1, self.leader1) if winner == 1 else (self.team2, self.leader2)
        )
        lteam, lleader = (
            (self.team2, self.leader2) if winner == 1 else (self.team1, self.leader1)
        )
        e = discord.Embed(
            title=f"🏆 Team {winner} Wins!",
            description=f"Map: **{self.map_played}**",
            color=discord.Color.green(),
        )
        e.add_field(
            name=f"Winners (+{config.WIN_POINTS} pts each)",
            value=self._fmt_team(wteam, wleader),
            inline=False,
        )
        e.add_field(name="Defeated", value=self._fmt_team(lteam, lleader), inline=False)
        e.set_footer(
            text="Points recorded — see /r6 leaderboard."
            if scored
            else "This match was already scored."
        )
        return e

    def _cancelled_embed(self, reason: str) -> discord.Embed:
        return discord.Embed(
            title="❌ Game Cancelled",
            description=reason,
            color=discord.Color.red(),
        )
