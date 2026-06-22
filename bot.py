"""Entrypoint for the Rainbow Six Siege match bot.

setup_hook order (sequence-sensitive): init DB -> reconcile orphaned matches ->
load the cog -> sync the command tree.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

import config
from database import Database
from game.manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("r6bot")


class R6Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True  # the only intent we need (slash + buttons)
        # We only use slash commands. Using when_mentioned (instead of a text
        # prefix) tells discord.py we don't need the message_content intent and
        # suppresses the misleading "commands may not work" warning.
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, help_command=None
        )
        self.db = Database(config.DB_PATH)
        self.manager = SessionManager()

    async def setup_hook(self):
        await self.db.init()
        cancelled = await self.db.reconcile_orphans()
        if cancelled:
            log.info("Reconciled %d orphaned match(es) from a previous run", cancelled)

        await self.load_extension("cogs.r6")

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to dev guild %s", len(synced), config.DEV_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d command(s) globally (may take up to 1h to appear)", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def close(self):
        await self.db.close()
        await super().close()


async def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    bot = R6Bot()

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: Exception
    ):
        log.exception("Unhandled app command error", exc_info=error)
        msg = "Something went wrong handling that. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
