#!/usr/bin/env python
"""Regenerate the MCP manifests from upstream, and report what moved.

Development tooling, not shipped in the wheel --- ``hatch`` packages only
``src/altus``, and this writes files in the repository.

The point of it is that the MCP manifests were the one classification input
Altus could not derive. Some of them still cannot be, but not all: GitHub's
server checks a JSON snapshot of every tool into its own repository, generated
from its source by its own test suite, carrying the annotations and the input
schema. That is a corpus in the same sense botocore is one, and reading it beats
anybody's reading of a README --- when the manifest was written by hand from the
published documentation it came out 40 tools short of 122.

Three source levels, and the generator never lets them blur:

    derived     an upstream machine-readable artifact at a pinned ref
    documented  the vendor's published tool table
    curated     a human, because neither of the above exists

Usage:
    uv run python scripts/refresh_manifests.py            # rewrite manifests
    uv run python scripts/refresh_manifests.py --check    # report, change nothing
    uv run python scripts/refresh_manifests.py github     # one server
"""

from __future__ import annotations

import argparse
import ast
import base64
import datetime as dt
import json
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFESTS = Path(__file__).resolve().parent.parent / "src" / "altus" / "mcp" / "manifests"

#: Pinned to a release rather than a branch, so a refresh is reproducible and
#: bumping it is a reviewable change rather than something that moves underneath
#: the manifest.
GITHUB_REF = "v1.12.1"
GITHUB_REPO = "github/github-mcp-server"
GITHUB_SNAPS = f"repos/{GITHUB_REPO}/contents/pkg/github/__toolsnaps__"

FALCON_REF = "v0.19.0"
FALCON_REPO = "CrowdStrike/falcon-mcp"
FALCON_MODULES = "falcon_mcp/modules"

#: falcon-mcp registers every tool as ``f"falcon_{name}"`` (modules/base.py).
#: The module source declares ``search_detections``; the server publishes
#: ``falcon_search_detections``. A manifest built from the unprefixed names
#: would match nothing at all, which is a failure that looks exactly like a
#: server with 166 unknown tools.
FALCON_PREFIX = "falcon_"


class RefreshError(RuntimeError):
    """An adapter could not do its job.

    Always raised rather than returning nothing. A scraper that quietly
    produces an empty tool table looks exactly like a clean run, and would
    classify every tool on that server as privileged.
    """


@dataclass
class Generated:
    """What an adapter worked out, before any human judgement is applied."""

    tools: dict[str, str] = field(default_factory=dict)
    target_fields: tuple[str, ...] = ()
    upstream_ref: str = ""

    def __post_init__(self) -> None:
        if not self.tools:
            raise RefreshError("adapter produced no tools at all")


# --------------------------------------------------------------------- fetching


def _api(path: str) -> Any:
    """One GitHub API call, using gh's token when there is one."""
    import os
    import subprocess

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        found = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        token = found.stdout.strip()
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "altus-refresh-manifests",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RefreshError(f"GET {path} failed: {exc.code} {exc.reason}") from exc
    except OSError as exc:
        raise RefreshError(f"GET {path} failed: {exc}") from exc


# --------------------------------------------------------------------- adapters


#: Argument names that identify *where* a call lands, most significant first.
#: Ordered because a CloudTarget renders context, then location, then scope.
TARGET_CANDIDATES = ("owner", "org", "organization", "repo", "repository", "branch")


def github_adapter() -> Generated:
    """From `pkg/github/__toolsnaps__/*.snap` --- one JSON file per tool.

    The read/write split comes from `annotations.readOnlyHint`, which is the
    server's own enforcement boundary rather than a description of it: passing
    `--read-only` drops exactly the tools that declare it false.
    """
    listing = _api(f"{GITHUB_SNAPS}?ref={GITHUB_REF}")
    if not isinstance(listing, list) or not listing:
        raise RefreshError("no tool snapshots found --- has the path moved?")

    tools: dict[str, str] = {}
    seen_fields: set[str] = set()
    for entry in listing:
        name = str(entry.get("name", ""))
        if not name.endswith(".snap"):
            continue
        blob = _api(f"repos/{GITHUB_REPO}/git/blobs/{entry['sha']}")
        snap = json.loads(base64.b64decode(blob["content"]))
        tool = name.removesuffix(".snap")
        notes = snap.get("annotations") or {}
        read_only = notes.get("readOnlyHint")
        if read_only is None:
            raise RefreshError(f"{tool}: no readOnlyHint --- the snapshot format changed")
        tools[tool] = "read" if read_only else "mutate"
        properties = (snap.get("inputSchema") or {}).get("properties") or {}
        seen_fields |= set(properties)

    fields = tuple(f for f in TARGET_CANDIDATES if f in seen_fields)
    return Generated(tools=tools, target_fields=fields, upstream_ref=GITHUB_REF)


def _falcon_sources(path: str) -> list[tuple[str, str]]:
    """Every module under ``path``, recursing into the ``cloud`` subpackage."""
    out: list[tuple[str, str]] = []
    listing = _api(f"repos/{FALCON_REPO}/contents/{path}?ref={FALCON_REF}")
    if not isinstance(listing, list) or not listing:
        raise RefreshError(f"{path} is empty or missing --- has the layout moved?")
    for entry in listing:
        if entry.get("type") == "dir":
            out += _falcon_sources(f"{path}/{entry['name']}")
            continue
        name = str(entry.get("name", ""))
        if not name.endswith(".py") or name == "__init__.py":
            continue
        blob = _api(f"repos/{FALCON_REPO}/git/blobs/{entry['sha']}")
        out.append((entry["path"], base64.b64decode(blob["content"]).decode("utf-8")))
    return out


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def crowdstrike_adapter() -> Generated:
    """From the module sources --- ``self._add_tool(name=..., annotations=...)``.

    Parsed as a syntax tree rather than with a regex, because the same modules
    build ``TextResource(name=...)`` objects for their FQL guides. Those are
    MCP *resources*, not tools, they go through a different method, and a
    regex over ``name="..."`` would have swept every one of them into the tool
    table.

    The read/write split is the ``annotations`` argument, defaulting to
    read-only exactly as ``_add_tool`` does (``annotations or
    READ_ONLY_ANNOTATIONS``). It is a ceiling and not a floor: CrowdStrike
    marks Real Time Response commands read-only because the *command* only
    reads, and ``[overrides]`` promotes them back to privileged.
    """
    tools: dict[str, str] = {}
    for path, text in _falcon_sources(FALCON_MODULES):
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise RefreshError(f"{path} could not be parsed: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "_add_tool"):
                continue
            name = _keyword(node, "name")
            if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                raise RefreshError(f"{path}: a tool name is not a literal --- cannot derive it")
            notes = _keyword(node, "annotations")
            read_only = True
            if notes is not None:
                if not isinstance(notes, ast.Call):
                    raise RefreshError(f"{path}: {name.value} annotations are not a literal call")
                hint = _keyword(notes, "readOnlyHint")
                read_only = bool(hint.value) if isinstance(hint, ast.Constant) else True
            tools[f"{FALCON_PREFIX}{name.value}"] = "read" if read_only else "mutate"

    # `target_fields` is empty on purpose, and it is the one manifest where
    # that is a finding rather than an omission. Every identifying argument
    # Falcon takes is an opaque id --- `ids`, `session_id`, `policy_type` ---
    # so a `*prod*` pattern would match nothing. The blast radius of a Falcon
    # call is the tenant, which is named by FALCON_BASE_URL and not by any
    # argument, so protection is matched on the configured scope instead.
    return Generated(tools=tools, target_fields=(), upstream_ref=FALCON_REF)


#: Servers with no machine-readable upstream. Left exactly as written, and
#: reported as such, rather than being quietly refreshed from nothing.
NO_ADAPTER = {
    "servicenow": "tool names come from the instance's own role-based tool packages",
    "atlassian": "Atlassian publishes permission scopes, not tool names",
    "snowflake": "tool names are chosen per deployment by whoever created the server object",
    "databricks": "tool names are the customer's own Genie spaces, indexes and UDFs",
    "grafana": "tools are published as a documentation table; no machine-readable artifact",
    "datadog": "tools are published as a documentation table; no machine-readable artifact",
    "newrelic": "tools are published as a documentation table; no machine-readable artifact",
}

ADAPTERS: dict[str, Any] = {"github": github_adapter, "crowdstrike": crowdstrike_adapter}


# --------------------------------------------------------------------- writing


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(server: str, existing: dict[str, Any], made: Generated) -> str:
    """The new TOML, preserving everything a human decided.

    ``[overrides]``, ``[before]`` and the header prose survive verbatim. The
    generator owns ``[tools]`` and nothing else.
    """
    overrides = dict(existing.get("overrides", {}))
    unknown = sorted(set(overrides) - set(made.tools))
    if unknown:
        raise RefreshError(
            f"{server}: overrides name tools that no longer exist upstream: "
            f"{', '.join(unknown)}. A rename here is a deliberate decision that "
            f"would be silently dropped, so this needs a human."
        )

    today = dt.date.today().isoformat()
    head = existing.get("_header", "")
    lines = [head.rstrip("\n"), ""] if head else []
    lines += [
        'source = "derived"',
        f"recorded = {_quote(today)}",
        f"upstream_ref = {_quote(made.upstream_ref)}",
        f"reference = {_quote(str(existing.get('reference', '')))}",
        "target_fields = [" + ", ".join(_quote(f) for f in made.target_fields) + "]",
        "",
        "[tools]",
        "# Generated. Edit [overrides] instead --- this table is rewritten in full",
        "# by scripts/refresh_manifests.py.",
    ]
    lines += [f"{name} = {_quote(level)}" for name, level in sorted(made.tools.items())]

    if overrides:
        lines += [
            "",
            "[overrides]",
            *[f"{k} = {_quote(str(v))}" for k, v in sorted(overrides.items())],
        ]
    before = {k: v for k, v in dict(existing.get("before", {})).items() if k in made.tools}
    if before:
        lines += ["", "[before]", *[f"{k} = {_quote(v)}" for k, v in sorted(before.items())]]
    return "\n".join(lines) + "\n"


def header_of(text: str) -> str:
    """The leading comment block, which is prose a human wrote."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            out.append(line)
        elif not line.strip() and out:
            break
        else:
            break
    return "\n".join(out)


def refresh(server: str, *, check: bool) -> int:
    """Returns 0 for clean, 1 for drift, 2 for a broken adapter."""
    path = MANIFESTS / f"{server}.toml"
    text = path.read_text(encoding="utf-8")
    existing = tomllib.loads(text)
    existing["_header"] = header_of(text)

    adapter = ADAPTERS.get(server)
    if adapter is None:
        # Report the manifest's own provenance rather than assuming. `documented`
        # and `curated` are different promises and must not print the same.
        source = str(existing.get("source", "curated"))
        print(f"  {server:<11} {source:<11} {NO_ADAPTER.get(server, 'no adapter')}")
        return 0

    try:
        made = adapter()
    except RefreshError as exc:
        print(f"  {server:<11} FAILED      {exc}")
        return 2

    was = {k: str(v) for k, v in dict(existing.get("tools", {})).items()}
    added = sorted(set(made.tools) - set(was))
    removed = sorted(set(was) - set(made.tools))
    changed = sorted(k for k in set(was) & set(made.tools) if was[k] != made.tools[k])

    if not (added or removed or changed):
        print(f"  {server:<11} clean       {len(made.tools)} tools, ref {made.upstream_ref}")
        return 0

    print(f"  {server:<11} DRIFT       +{len(added)} -{len(removed)} ~{len(changed)}")
    for name in added:
        print(f"      + {name} ({made.tools[name]})")
    for name in removed:
        print(f"      - {name}")
    for name in changed:
        print(f"      ~ {name}: {was[name]} -> {made.tools[name]}")

    if not check:
        path.write_text(render(server, existing, made), encoding="utf-8")
        print(
            f"      wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}"
        )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("servers", nargs="*", help="servers to refresh (default: all)")
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args()

    servers = args.servers or sorted(p.stem for p in MANIFESTS.glob("*.toml"))
    print("Refreshing MCP manifests" + (" (check only)" if args.check else ""))
    codes = [refresh(server, check=args.check) for server in servers]
    if 2 in codes:
        print("\nAn adapter failed. This is NOT 'no drift' --- nothing was verified.")
        return 2
    if 1 in codes:
        print("\nDrift found." + (" Re-run without --check to apply." if args.check else ""))
        return 1
    print("\nClean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
