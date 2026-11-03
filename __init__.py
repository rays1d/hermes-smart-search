"""Smart Search web provider plugin.

Registers:
- the ``smartsearch`` web backend (``web_search`` / ``web_extract``)
- the ``web_research`` tool (bounded multi-round research with citations)
"""

from __future__ import annotations

from .provider import RESEARCH_SCHEMA, SmartSearchProvider

_provider = SmartSearchProvider()


def register(ctx) -> None:
    ctx.register_web_search_provider(_provider)
    ctx.register_tool(
        name=RESEARCH_SCHEMA["name"],
        toolset="web",
        schema=RESEARCH_SCHEMA,
        handler=lambda args, **kwargs: _provider.research_json(args),
        check_fn=_provider.is_available,
        description="Deep multi-round web research with citations (smart-search)",
        emoji="🔬",
    )
