"""How dangerous is an MCP tool?

The same four levels every other surface uses, decided from a curated manifest
rather than a corpus --- because there is no corpus to read. A manifest entry
is the primary answer; everything else in here either escalates it or fails it
closed.

Two rules carry most of the weight.

**A server's own annotations may raise sensitivity and may never lower it.**
MCP lets a server declare ``readOnlyHint`` and ``destructiveHint``, and the
specification is explicit that a client must not rely on those from a server it
does not trust. Treating them as a ceiling rather than a floor gets the useful
half of the signal without making a remote process the author of our safety
policy. Datadog is the worked example: it labels ``execute_code`` and
``datadog_remote_action_restricted_shell_run_command`` read-only, and both run
code somewhere else. Altus calls them privileged.

**A tool that is not in the manifest is privileged.** That is the drift alarm.
A name we have never seen is, almost by definition, a server that shipped a
release since the manifest was written.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from altus.cloud.base import CloudTarget, Sensitivity
from altus.mcp.catalog import DATABRICKS_SCOPES, server_spec

MANIFESTS = Path(__file__).parent / "manifests"

_RANK: dict[Sensitivity, int] = {
    Sensitivity.READ: 0,
    Sensitivity.SENSITIVE_READ: 1,
    Sensitivity.MUTATE: 2,
    Sensitivity.PRIVILEGED: 3,
}


def _stricter(a: Sensitivity, b: Sensitivity) -> Sensitivity:
    return a if _RANK[a] >= _RANK[b] else b


#: Names meaning "run whatever you are given, somewhere else". Privileged for
#: the reason `k8s_exec` is: the tool's own verb says nothing about the blast
#: radius, because the caller supplies it.
ARBITRARY_EXECUTION = re.compile(
    r"(^|_)(execute_code|execute_sql|system_execute_sql|run_command|run_script"
    r"|submit_job|shell_run|eval)(_|$)|_shell_",
    re.IGNORECASE,
)

#: Rewriting who may do what. The MCP analogue of `setIamPolicy` and RBAC.
AUTHZ = re.compile(
    r"(permission|ruleset|branch_protection|_iam_|collaborator|_role|_member"
    r"|access_control|grant|revoke)",
    re.IGNORECASE,
)

#: Reads that return credential material rather than describing it.
CREDENTIAL_READ = re.compile(
    r"(token|secret|credential|api_key|apikey|private_key|password)",
    re.IGNORECASE,
)

#: Deleting one of these is not an edit. Deliberately tighter than "anything
#: deletable": a Grafana annotation and a GitHub repository are both deletable,
#: and demanding a typed challenge for both is how a challenge stops working.
STATEFUL = re.compile(
    r"(repositor|repo|branch|file|dashboard|database|schema|table|index|warehouse"
    r"|cluster|workspace|project|space|catalog|volume|bucket|environment|user|team|org)",
    re.IGNORECASE,
)

DESTROY = re.compile(r"(^|_)(delete|destroy|drop|purge|remove|erase)(_|$)", re.IGNORECASE)


def _hint(notes: Any, *names: str) -> bool | None:
    for name in names:
        found = getattr(notes, name, None)
        if found is not None:
            return bool(found)
    return None


@dataclass(frozen=True)
class ToolInfo:
    """A tool as the server described it, annotations included."""

    name: str
    description: str = ""
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None

    @property
    def server_says_write(self) -> bool:
        return self.read_only_hint is False or self.destructive_hint is True

    @classmethod
    def from_mcp(cls, tool: Any) -> ToolInfo:
        """Build from the SDK's ``Tool``, which may or may not carry annotations.

        Both spellings are read. The protocol wire format is ``readOnlyHint``,
        the installed Python SDK exposes the field as ``read_only_hint``, and
        reading only one of them means every annotation silently arrives as
        ``None`` --- which would quietly disable the escalation rule rather
        than break anything visibly.
        """
        notes = getattr(tool, "annotations", None)
        return cls(
            name=str(getattr(tool, "name", "")),
            description=str(getattr(tool, "description", "") or ""),
            read_only_hint=_hint(notes, "read_only_hint", "readOnlyHint"),
            destructive_hint=_hint(notes, "destructive_hint", "destructiveHint"),
        )


@dataclass(frozen=True)
class Manifest:
    """One server's table, as shipped, and where it came from."""

    server: str
    source: str = "curated"
    """How this table was built, and the three are never conflated:

    ``derived``     generated from an upstream machine-readable artifact at a
                    pinned ref --- evidence, and refreshable by a script.
    ``documented``  parsed from the vendor's published tool table --- weaker,
                    because docs lag releases and get restyled.
    ``curated``     written by hand, because no machine-readable source exists.

    The split exists so the three hand-written manifests cannot hide among the
    generated ones.
    """
    recorded: str = ""
    reference: str = ""
    upstream_ref: str = ""
    """The tag or commit the generated entries were read from."""
    tools: dict[str, Sensitivity] = field(default_factory=dict)
    overrides: dict[str, Sensitivity] = field(default_factory=dict)
    """Decisions a human made that regeneration must never undo.

    The generator decides only whether a tool reads or writes. Everything that
    needed judgement --- Datadog calling its remote shell tool read-only, for
    one --- lives here, above ``tools`` in precedence, and is preserved across
    every refresh.
    """
    before: dict[str, str] = field(default_factory=dict)
    """Mutating tool -> the read that fetches its current state. The nearest
    thing to a preflight this surface has."""
    target_fields: tuple[str, ...] = ()
    """Argument names, in order, that locate the blast radius."""

    def get(self, tool: str) -> Sensitivity | None:
        found = self.overrides.get(tool)
        return found if found is not None else self.tools.get(tool)

    def knows(self, tool: str) -> bool:
        return tool in self.overrides or tool in self.tools


@cache
def manifest(server: str) -> Manifest:
    path = MANIFESTS / f"{server}.toml"
    if not path.is_file():
        return Manifest(server=server)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return Manifest(
        server=server,
        source=str(raw.get("source", "curated")),
        recorded=str(raw.get("recorded", "")),
        reference=str(raw.get("reference", "")),
        upstream_ref=str(raw.get("upstream_ref", "")),
        tools={k: Sensitivity(v) for k, v in dict(raw.get("tools", {})).items()},
        overrides={k: Sensitivity(v) for k, v in dict(raw.get("overrides", {})).items()},
        before=dict(raw.get("before", {})),
        target_fields=tuple(raw.get("target_fields", ())),
    )


def _unknown(server: str, tool: str, scope: str) -> tuple[Sensitivity, str]:
    """What an unlisted tool classifies as, and why --- the prompt says the why.

    Databricks is the case that needs a scope: capability there is a property
    of the endpoint, not of the name. A vector-search endpoint exposes one
    query per index and cannot do anything else, so failing it closed would
    demand a typed challenge to run a search.
    """
    if server == "databricks" and scope:
        head = scope.strip("/").split("/", 1)[0]
        found = DATABRICKS_SCOPES.get(head)
        if found is not None:
            return found
    spec = server_spec(server)
    if spec is None:
        return Sensitivity.PRIVILEGED, f"{server!r} is not a server Altus ships"
    return spec.unknown, spec.unknown_why


def classify(
    server: str,
    tool: str,
    info: ToolInfo | None = None,
    *,
    scope: str = "",
) -> Sensitivity:
    """Where this call sits on the four-level scale.

    ``info`` is what the server said about itself. It can only make the answer
    stricter.
    """
    base = manifest(server).get(tool)
    if base is None:
        base, _ = _unknown(server, tool, scope)

    if ARBITRARY_EXECUTION.search(tool):
        base = Sensitivity.PRIVILEGED

    writes = _RANK[base] >= _RANK[Sensitivity.MUTATE]
    if info is not None and info.server_says_write and not writes:
        # The manifest calls it a read and the server calls it a write. Believe
        # whichever is worse, and let `drift` surface the disagreement.
        base = _stricter(base, Sensitivity.MUTATE)
        writes = True

    if writes and AUTHZ.search(tool):
        return Sensitivity.PRIVILEGED
    if writes and DESTROY.search(tool) and STATEFUL.search(tool):
        return Sensitivity.PRIVILEGED
    if not writes and CREDENTIAL_READ.search(tool):
        return _stricter(base, Sensitivity.SENSITIVE_READ)
    return base


def why_unknown(server: str, tool: str, scope: str = "") -> str:
    """The sentence a prompt uses when the manifest has never seen this tool."""
    if manifest(server).knows(tool):
        return ""
    _, reason = _unknown(server, tool, scope)
    return reason


def drift(server: str, tools: list[ToolInfo], *, scope: str = "") -> list[str]:
    """Where the live server and the shipped manifest disagree.

    Reported rather than silently absorbed: an unlisted tool means the vendor
    shipped something, and a tool the server calls a write while the manifest
    calls it a read means the manifest is wrong in the dangerous direction.
    """
    out: list[str] = []
    table = manifest(server)
    for tool in tools:
        listed = table.get(tool.name)
        if listed is None:
            level, reason = _unknown(server, tool.name, scope)
            out.append(f"{tool.name}: not in the manifest, treated as {level} --- {reason}")
        elif tool.server_says_write and _RANK[listed] < _RANK[Sensitivity.MUTATE]:
            out.append(
                f"{tool.name}: manifest says {listed}, the server declares it a write "
                f"--- escalated to {classify(server, tool.name, tool, scope=scope)}"
            )
    return out


@dataclass(frozen=True)
class Target:
    """Where a call would land, and whether that could be worked out at all.

    The second half is the point. An earlier version returned a bare
    ``CloudTarget`` with empty fields when nothing matched, which meant
    ``ProtectionRules`` matched nothing and protection silently did not fire ---
    a fail-open, on the one control that is supposed to stop a production
    mistake. A target that could not be resolved is now its own state, and the
    gate treats it as protected.
    """

    target: CloudTarget
    resolved: bool

    def render(self) -> str:
        return self.target.render() if self.resolved else f"{self.target.cloud}: (unknown)"

    @property
    def unknown_reason(self) -> str:
        if self.resolved:
            return ""
        return (
            "the blast radius of this call could not be determined from its "
            "arguments, so it is treated as protected"
        )


def target_for(server: str, args: dict[str, Any], *, scope: str = "") -> Target:
    """Where this call would land.

    Built from the manifest's ``target_fields`` so ``ProtectionRules`` --- and
    the default ``*prod*`` pattern that ships with it --- applies to MCP calls
    with no new configuration.
    """
    fields = manifest(server).target_fields
    parts = [str(args[name]) for name in fields if args.get(name) not in (None, "")]
    if scope:
        parts.append(scope)
    resolved = bool(parts)
    parts += [""] * 3
    return Target(
        target=CloudTarget(cloud=server, context=parts[0], location=parts[1], scope=parts[2]),
        resolved=resolved,
    )
