"""Tool lookup and dispatch.

``execute`` never raises. Every failure --- unknown tool, bad arguments, a
denied path, an OS error --- comes back as an error ``ToolOutcome`` so the
agent loop can hand it to the model as an error ``tool_result``. A raised
exception would abort a turn the model could have recovered from.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

from altus.core.errors import PathNotAllowed, ToolError
from altus.core.types import ToolDef
from altus.tools.base import Tool, ToolContext, ToolOutcome
from altus.tools.edit import DeletePathTool, EditFileTool, WriteFileTool
from altus.tools.fs import GlobTool, GrepTool, ListDirTool, ReadFileTool

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def add(self, tool: Tool) -> None:
        """Offer a tool for part of a session.

        ``workflow_save`` is registered only while the user is drafting one,
        because a tool that writes executable workflow files should not sit in
        the list for every unrelated turn.
        """
        self._tools[tool.name] = tool

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def is_read_only(self, name: str) -> bool:
        """Unknown tools count as mutating, so they take the cautious path."""
        tool = self._tools.get(name)
        return bool(tool and tool.read_only)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def to_tool_defs(self) -> list[ToolDef]:
        """The provider-facing declarations, in a stable order."""
        return [
            ToolDef(name=tool.name, description=tool.description, input_schema=tool.input_schema)
            for tool in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome.error(
                f"unknown tool {name!r}. Available: {', '.join(self.names) or 'none'}",
                summary="unknown tool",
            )
        if not isinstance(args, dict):
            return ToolOutcome.error("tool arguments must be an object", summary="bad arguments")
        try:
            return await tool.run(args, ctx)
        except PathNotAllowed as exc:
            return ToolOutcome.error(str(exc), summary="denied")
        except ToolError as exc:
            return ToolOutcome.error(str(exc), summary="failed")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("tool %s failed: %s", name, exc, exc_info=True)
            return ToolOutcome.error(f"{name} failed: {exc}", summary="failed")


def default_registry(
    *,
    writes: bool = True,
    git: bool = True,
    kubernetes: bool | None = None,
    aws: bool | None = None,
    azure: bool | None = None,
    gcp: bool | None = None,
    mcp: bool | None = None,
    cloud: Any = None,
    mcp_settings: Any = None,
) -> ToolRegistry:
    """The tool set for a session.

    Kubernetes tools register only when the SDK is installed, so a missing
    extra is a visible absence rather than an import error at call time. The
    same pattern applies one level down: ``[cloud.k8s]`` switches off whole
    capability classes, and a class that is off is never registered, so the
    model is not told it exists.

    ``cloud`` is a ``CloudSettings``; None means every class is on.

    ``kubernetes``, ``aws``, ``azure``, ``gcp`` and ``mcp`` default to
    autodetection from what is installed. Pass False for any of them to build a
    registry without it --- which is what a test wanting only the filesystem
    tools should do.

    ``mcp_settings`` is an ``McpSettings``; it lives outside ``CloudSettings``
    because MCP is not a cloud.

    ``git`` registers the local git tools. They need no SDK and no credentials
    --- only a `git` binary, which every path here already assumes --- so they
    are on by default and `[tools] git = false` is how a session opts out.
    """
    tools: list[Tool] = [ReadFileTool(), ListDirTool(), GlobTool(), GrepTool()]
    if writes:
        tools += [WriteFileTool(), EditFileTool(), DeletePathTool()]
    if git:
        from altus.tools.git import git_tools

        tools += git_tools(writes=writes)
    # Cloud tools are assembled below and filtered at the end, because their
    # own switches decide registration first and `writes` is the floor.

    if kubernetes is None:
        from altus.cloud.base import integration

        entry = integration("k8s")
        kubernetes = bool(entry and entry.available)
    if kubernetes:
        from altus.tools.k8s import k8s_tools

        tools += list(k8s_tools(getattr(cloud, "k8s", None)))

    if aws is None:
        from altus.cloud.base import integration

        aws_entry = integration("aws")
        aws = bool(aws_entry and aws_entry.available)
    if aws:
        from altus.tools.aws import aws_tools

        tools += list(aws_tools(getattr(cloud, "aws", None)))

    if azure is None:
        from altus.cloud.base import integration

        azure_entry = integration("azure")
        azure = bool(azure_entry and azure_entry.available)
    if azure:
        from altus.tools.azure import azure_tools

        tools += list(azure_tools(getattr(cloud, "azure", None)))

    if gcp is None:
        from altus.cloud.base import integration

        gcp_entry = integration("gcp")
        gcp = bool(gcp_entry and gcp_entry.available)
    if gcp:
        from altus.tools.gcp import gcp_tools

        tools += list(gcp_tools(getattr(cloud, "gcp", None)))

    if mcp is None:
        from altus.cloud.base import integration

        mcp_entry = integration("mcp")
        mcp = bool(mcp_entry and mcp_entry.available)
    if mcp:
        from altus.tools.mcp import mcp_tools

        tools += list(mcp_tools(mcp_settings))

    if _cli_enabled(cloud):
        from altus.tools.cli import cli_tools

        allowed = set(getattr(cloud, "cli_allowlist", ()) or ())
        blocked = _cli_blocked(cloud)
        tools += [
            t
            for t in cli_tools()
            if (not allowed or t.binary in allowed) and t.binary not in blocked
        ]

    if not writes:
        # `writes=False` has to mean *no writes*, not "no file writes". It
        # filtered only the filesystem tools, so a read-only registry still
        # carried k8s_delete and aws_write --- and build_system_prompt then
        # told the model it had read-only access while handing it a drain.
        tools = [tool for tool in tools if tool.read_only]
    return ToolRegistry(tools)


def _cli_enabled(cloud: Any) -> bool:
    """Two switches have to agree: the long-standing `cli_fallback`, and the
    Kubernetes-specific `allow_cli`. Either one off means no CLI tools."""
    if cloud is None:
        return False
    if not getattr(cloud, "cli_fallback", False):
        return False
    return bool(getattr(getattr(cloud, "k8s", None), "allow_cli", True))


def _cli_blocked(cloud: Any) -> set[str]:
    """Binaries a per-cloud `allow_cli` switches off on its own.

    Kubernetes gates the whole CLI layer because that switch predates the
    others; Azure only gates `az`, so turning it off does not take kubectl
    with it."""
    blocked: set[str] = set()
    if not getattr(getattr(cloud, "azure", None), "allow_cli", True):
        blocked.add("az")
    if not getattr(getattr(cloud, "gcp", None), "allow_cli", True):
        blocked.add("gcloud")
    return blocked
