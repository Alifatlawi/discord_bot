"""Result buttons: each leader reports the winning team.

Points are committed only when both leaders report the same team (enforced in the
session under its lock). timeout=None — the result clock is a session asyncio timer.
"""

from __future__ import annotations

import discord


class ResultView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="Team 1 won", style=discord.ButtonStyle.primary, emoji="🔵")
    async def team1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.session.handle_report(interaction, 1)

    @discord.ui.button(label="Team 2 won", style=discord.ButtonStyle.primary, emoji="🔴")
    async def team2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.session.handle_report(interaction, 2)
