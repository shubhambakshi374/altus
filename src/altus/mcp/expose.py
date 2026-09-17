"""Which of Altus's own tools it is honest to offer down a wire.

Everything else in this package points outwards: a catalogue of vendor servers,
a manifest saying what their tools do, a classifier that fails closed. This
module points the other way --- Altus as the server --- and the question it
answers is deliberately narrower than "what is in the registry".

The reason is that a server is a process holding credentials. In the TUI the
person calling a tool is the person whose keys it uses; over MCP they are not.
A client that could call ``mcp_do`` would reach CrowdStrike through *our*
credentials, outside whatever policy that client believed it had --- the
textbook confused deputy, and no approval prompt fixes it, because the prompt
would be answered by somebody who was told only what the calling model chose to
say.

So there are three tiers, and the difference between the second and third is
the load-bearing part:

``NEVER``     not offered under any configuration. ``extra`` cannot reach into
              it, because an allowlist a config file can talk past is not one.
``DROPPED``   not offered by default, with a reason a caller can read, and
              re-addable by name through ``extra``.
everything else, filtered by ``allow_writes`` and ``deny``.
"""

from __future__ import annotations

from typing import Any

from altus.cloud.base import Sensitivity
from altus.tools.base import sensitivity_of

#: Not offered under any configuration.
NEVER: dict[str, str] = {
    # --- relaying to other people's servers with our credentials
    "mcp_do": "it would relay writes to vendor servers using Altus's credentials",
    "mcp_call": "it would relay calls to vendor servers using Altus's credentials",
    "mcp_tools": "it enumerates vendor servers Altus is configured for, which is "
    "not this client's business",
    "mcp_servers": "it enumerates vendor servers Altus is configured for, which is "
    "not this client's business",
    # --- arbitrary execution on this host. The CLI fallbacks --- kubectl,
    # helm, aws, az, gcloud --- are caught structurally by `_shells_out` rather
    # than by name, so a binary added to that layer later is covered without
    # anybody remembering to add it here.
    "shell_run": "the shell allowlist was written for a person at the TUI, not for a remote caller",
    "k8s_exec": "running a command inside a container reaches whatever that container can reach",
    "k8s_cp": "copying files in and out of a container is execution by another name",
    "k8s_port_forward": "it opens a tunnel from this machine into a cluster network",
    # --- writing things that decide what runs later
    "workflow_save": "a client that can write a workflow file can write one that "
    "does anything, then ask a human to approve a name",
    # --- the filesystem, whose reads are not offered either
    "write_file": "the filesystem reads are not offered, so the writes cannot coherently be",
    "edit_file": "the filesystem reads are not offered, so the writes cannot coherently be",
    "delete_path": "the filesystem reads are not offered, so the writes cannot coherently be",
    # --- retargeting the whole connection
    "k8s_use_context": "it retargets every later call on this connection, and no "
    "later prompt would mention that it happened",
}

#: Not offered by default. ``extra`` can add any of these back by name.
DROPPED: dict[str, str] = {
    "read_file": "every client has its own, and ours answers against a workspace "
    "sandbox this one cannot see",
    "list_dir": "every client has its own, and ours answers against a workspace "
    "sandbox this one cannot see",
    "glob": "every client has its own, and ours answers against a workspace "
    "sandbox this one cannot see",
    "grep": "every client has its own, and ours answers against a workspace "
    "sandbox this one cannot see",
    "k8s_raw": "a GET to an arbitrary API path: read-only, but its blast radius "
    "cannot be worked out from the tool name",
}

#: Prefixes whose tools act on something outside this machine. MCP calls that
#: ``openWorldHint``, and it is the difference between "this reads a file" and
#: "this asks a cluster in another country".
OPEN_WORLD = ("k8s", "aws", "azure", "gcp", "git", "mcp")


def _shells_out(tool: Any) -> str:
    """Why this tool runs a binary, or "" if it does not.

    The CLI fallback layer hands an argv to a binary on this host. Its
    allowlist, like the shell tool's, was written for a person at a terminal
    who can see what is about to run; a remote caller is not that person.
    """
    from altus.tools.cli import CliTool

    if not isinstance(tool, CliTool):
        return ""
    return (
        f"it shells out to {tool.binary}, and the allowlist behind that was "
        "written for a person at the TUI, not for a remote caller"
    )


def _allow_writes(settings: Any) -> bool:
    return bool(getattr(settings, "allow_writes", False))


def _names(settings: Any, field: str) -> set[str]:
    return {str(name) for name in (getattr(settings, field, ()) or ())}


def exposed(registry: Any, settings: Any = None) -> list[Any]:
    """The tools this server offers, in name order.

    Altus tools, not MCP ones --- the protocol shapes are built in ``serve``,
    so this stays testable without the SDK and without a transport.
    """
    return sorted(
        (tool for tool in registry if not why_not(tool.name, settings, registry)),
        key=lambda tool: tool.name,
    )


def why_not(name: str, settings: Any = None, registry: Any = None) -> str:
    """Why ``name`` is not offered, or "" when it is.

    A caller that names a withheld tool is told *that* it was withheld and why,
    rather than "unknown tool" --- which would be a lie about a tool that
    exists, and would send them looking for a typo.
    """
    if name in NEVER:
        return NEVER[name]
    if registry is not None and name not in registry:
        return f"no tool called {name!r} exists in this session"
    if name in _names(settings, "deny"):
        return "it is named in [mcp.expose] deny"
    if name in DROPPED and name not in _names(settings, "extra"):
        return DROPPED[name] + " --- add it to [mcp.expose] extra to offer it anyway"

    tool = registry.get(name) if registry is not None else None
    if tool is not None and (shelled := _shells_out(tool)):
        return shelled
    if tool is not None and not tool.read_only and not _allow_writes(settings):
        return "it changes things, and [mcp.expose] allow_writes is false"
    return ""


def annotations_for(tool: Any) -> dict[str, bool]:
    """The MCP hints for one tool, computed from the same classifier as ``/tools``.

    Three phases of this codebase have treated other servers' annotations as a
    ceiling and never a floor, on the grounds that a remote process should not
    be the author of our safety policy. Ours are generated rather than typed,
    so a client applying the same suspicion to us gets the same answer either
    way: ``readOnlyHint`` here is ``sensitivity_of`` speaking outwards.
    """
    level = sensitivity_of(tool)
    return {
        "readOnlyHint": level is Sensitivity.READ,
        "destructiveHint": level is not Sensitivity.READ,
        "openWorldHint": tool.name.split("_", 1)[0] in OPEN_WORLD,
    }


def withheld(registry: Any, settings: Any = None) -> list[tuple[str, str]]:
    """``(name, reason)`` for everything this server will not offer.

    Printed by ``altus mcp expose``. A server whose surface you have to start it
    to discover is one nobody audits.
    """
    offered = {tool.name for tool in exposed(registry, settings)}
    return sorted(
        (tool.name, why_not(tool.name, settings, registry))
        for tool in registry
        if tool.name not in offered
    )
