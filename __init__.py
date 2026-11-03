"""Smart Search web provider plugin — registers the ``smartsearch`` backend."""

from __future__ import annotations

from .provider import SmartSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(SmartSearchProvider())
