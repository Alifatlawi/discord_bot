"""Lobby buttons: Join / Leave / Cancel.

timeout=None — the lobby clock is driven by the session's asyncio timer, so the
buttons never silently die. All logic lives in the session; this view is wiring.
"""

from __future__ import annotations

import discord


class LobbyView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.session.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.session.handle_leave(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.session.handle_cancel_button(interaction)
