"""Rainbow Six Siege map pool and slug helpers.

Edit ``MAP_POOL`` freely. The ban phase always alternates bans down to the single
surviving map, so it works for any pool of size >= 2. An ODD pool size (the 15-map
default) splits bans evenly between the two leaders.
"""

import re

# 2026 competitive pool. Edit to taste.
MAP_POOL: list[str] = [
    "Club House",
    "Bank",
    "Border",
    "Chalet",
    "Kafe Dostoyevsky",
    "Lair",
    "Nighthaven Labs",
    "Kanal",
    "Skyscraper",
    "Theme Park",
    "Outback",
    "Consulate",
    "Coastline",
    "Oregon",
    "Villa",
]


def slug(name: str) -> str:
    """A button-safe identifier for a map name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_SLUG_TO_MAP: dict[str, str] = {slug(m): m for m in MAP_POOL}


def unslug(value: str) -> str | None:
    """Resolve a slug back to its map name, or None if unknown."""
    return _SLUG_TO_MAP.get(value)
