"""Optional MCP transport — binds Mimir's tool surface onto a FastMCP server.

The core package stays dependency-free; the `mcp` SDK is imported lazily here so
you install it only when you actually serve. Tools whose handler is None (they
need injected LLM callables — `consolidate`/`attribute`) are skipped until that
funded wiring lands; `recall` and (with a log_path) `capture` register live.

    python -m mimir.serve            # serves over stdio (needs `pip install mcp`)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mimir.mcp_server import build_tools


def build_server(store, *, name: str = "mimir",
                 log_path: Optional[Path] = None, fastmcp=None):
    """Register every handler-bearing tool onto a FastMCP server and return it.

    Pass `fastmcp` to inject a server (tests / a custom transport); otherwise the
    real `mcp.server.fastmcp.FastMCP` is imported lazily and constructed.
    """
    if fastmcp is None:
        from mcp.server.fastmcp import FastMCP  # lazy: dep only needed to serve
        fastmcp = FastMCP(name)
    for tool in build_tools(store, log_path=log_path).values():
        if tool.handler is None:
            continue  # needs injected LLM callables; bound in a funded live run
        fastmcp.add_tool(tool.handler, name=tool.name, description=tool.description)
    return fastmcp


if __name__ == "__main__":  # pragma: no cover — needs the mcp SDK + a real store
    from mimir.store import InMemoryLessonStore

    build_server(InMemoryLessonStore(),
                 log_path=Path.home() / ".mimir" / "episodes.jsonl").run()
