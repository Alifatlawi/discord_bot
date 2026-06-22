"""Map-ban buttons: one danger button per remaining map, rebuilt each render.

The session's ``handle_ban`` is the single authority — it re-checks (under the
session lock) whose turn it is and whether the map is still available, so a stale
or double click is rejected ephemerally rather than mis-applied.

Discord component limits: 5 buttons/row, 5 rows, 25 components per message. The
default 15-map pool fits in 3 rows. If MAP_POOL is edited beyond 25 maps, switch
this to a discord.ui.Select.
"""

from __future__ import annotations

import discord

from game import maps


class _BanButton(discord.ui.Button):
    def __init__(self, session, map_name: str, row: int):
        super().__init__(
            label=map_name,
            style=discord.ButtonStyle.danger,
            custom_id=f"ban:{session.match_id}:{maps.slug(map_name)}",
            row=row,
        )
        self.session = session
        self.map_name = map_name

    async def callback(self, interaction: discord.Interaction):
        await self.session.handle_ban(interaction, self.map_name)


class BanView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        for i, map_name in enumerate(session.remaining[:25]):
            self.add_item(_BanButton(session, map_name, row=i // 5))
