"""MCP tools.

Four of them, whatever is connected. Seven servers publish well over two
hundred tools between them --- more schema than everything else Altus
registers put together --- so the inventory is searched rather than declared,
and ``mcp_tools`` is the only way the model learns any of it exists.
"""

from __future__ import annotations

from typing import Any

from altus.tools.base import BaseTool
from altus.tools.mcp.base import McpMutatingTool, McpTool
from altus.tools.mcp.mutations import McpDoTool
from altus.tools.mcp.reads import McpCallTool, McpServersTool, McpToolsTool


def mcp_tools(settings: Any = None) -> list[BaseTool]:
    """The tool set, minus whatever `[mcp]` switches off."""
    if settings is not None and not getattr(settings, "enabled", True):
        return []
    tools: list[BaseTool] = [McpServersTool(), McpToolsTool(), McpCallTool()]
    if settings is None or getattr(settings, "allow_writes", True):
        tools.append(McpDoTool())
    return tools


__all__ = ["McpMutatingTool", "McpTool", "mcp_tools"]
