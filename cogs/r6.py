"""The /r6 slash command group: start, leaderboard, rank, cancel."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class R6(commands.GroupCog, name="r6"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = bot.manager
        self.db = bot.db

    @app_commands.command(
        description="Start a new R6 custom match lobby (waits for 10 players)."
    )
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if self.manager.get(interaction.channel_id) is not None:
            await interaction.response.send_message(
                "A game is already running in this channel.", ephemeral=True
            )
            return

        session = self.manager.create(
            self.bot,
            interaction.guild_id,
            interaction.channel_id,
            interaction.channel,
            interaction.user.id,
            interaction.user.display_name,
        )
        await self.db.upsert_player(
            interaction.guild_id, interaction.user.id, interaction.user.display_name
        )
        await interaction.response.send_message(
            embed=session._lobby_embed(), view=session._lobby_view()
        )
        message = await interaction.original_response()
        await session.begin_lobby(message)

    @app_commands.command(description="Show the top players on this server.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.db.top_players(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "No matches have been completed yet.", ephemeral=True
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f"**{i + 1}.**"
            games = r["wins"] + r["losses"]
            pct = round(100 * r["wins"] / games, 1) if games else 0.0
            lines.append(
                f"{rank} {r['display_name']} — **{r['points']}** pts "
                f"({r['wins']}W-{r['losses']}L · {pct}%)"
            )
        embed = discord.Embed(
            title="🏆 R6 Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Show your own match stats.")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction):
        row = await self.db.get_player(interaction.guild_id, interaction.user.id)
        if row is None or row["matches_played"] == 0:
            await interaction.response.send_message(
                "You haven't completed any matches yet. Join a game with /r6 start!",
                ephemeral=True,
            )
            return
        games = row["wins"] + row["losses"]
        pct = round(100 * row["wins"] / games, 1) if games else 0.0
        embed = discord.Embed(
            title=f"📊 Stats for {row['display_name']}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Points", value=str(row["points"]))
        embed.add_field(name="Record", value=f"{row['wins']}W-{row['losses']}L")
        embed.add_field(name="Win rate", value=f"{pct}%")
        embed.add_field(name="Matches played", value=str(row["matches_played"]))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        description="Cancel the active game in this channel (starter or admin only)."
    )
    @app_commands.guild_only()
    async def cancel(self, interaction: discord.Interaction):
        session = self.manager.get(interaction.channel_id)
        if session is None:
            await interaction.response.send_message(
                "There is no active game in this channel.", ephemeral=True
            )
            return
        perms = interaction.user.guild_permissions
        if not (interaction.user.id == session.starter_id or perms.manage_guild):
            await interaction.response.send_message(
                "Only the game starter or a server admin can cancel.", ephemeral=True
            )
            return
        await interaction.response.send_message("Game cancelled.", ephemeral=True)
        await session.cancel_external(
            f"Cancelled by {interaction.user.display_name}."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(R6(bot))
