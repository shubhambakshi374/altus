"""What a workflow is run *against*.

Without these a workflow is one file per repository, which is not a workflow.
``${inputs.repo}`` is available to every step with no ``needs`` entry, because
an input is known before the first step runs rather than produced by one.

``@git.origin`` is the "current repo" affordance and the only dynamic default
there is. Everything else is a literal or is asked for. Keeping the list of
dynamic defaults to one entry is deliberate: each one is a thing that can
resolve differently on two machines, which is the property that makes a
workflow file stop being portable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from altus.core.errors import ConfigError

PREFIX = "inputs."

#: `git@github.com:owner/name.git`, `https://github.com/owner/name.git`, and
#: the ssh:// and plain forms of both.
REMOTE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")


def declared(workflow: Any) -> dict[str, Any]:
    return dict(getattr(workflow, "inputs", {}) or {})


def missing(workflow: Any, given: dict[str, str]) -> list[str]:
    """Required inputs with no value and no default. Asked for, never guessed."""
    return sorted(
        name
        for name, spec in declared(workflow).items()
        if spec.required and not given.get(name) and not spec.default
    )


def resolve(workflow: Any, given: dict[str, str], *, root: Path | None = None) -> dict[str, str]:
    """The final values, keyed as ``inputs.<name>`` ready for substitution.

    Raises rather than substituting an empty string for something required:
    approving a run whose targets were still blank would be approving nothing.
    """
    unknown = sorted(set(given) - set(declared(workflow)))
    if unknown:
        # Not ignored. A misspelt `repo=` that silently does nothing produces a
        # run against whatever the default was, which is the worst outcome.
        raise ConfigError(
            f"{workflow.name} declares no input called {', '.join(repr(u) for u in unknown)}. "
            f"It takes: {', '.join(sorted(declared(workflow))) or 'nothing'}"
        )
    absent = missing(workflow, given)
    if absent:
        raise ConfigError(f"{workflow.name} needs a value for: {', '.join(absent)}")

    out: dict[str, str] = {}
    for name, spec in declared(workflow).items():
        value = given.get(name) or spec.default
        out[f"{PREFIX}{name}"] = _dynamic(value, root) if value.startswith("@") else value
    return out


def _dynamic(value: str, root: Path | None) -> str:
    if value == "@git.origin":
        return git_origin(root or Path.cwd())
    raise ConfigError(f"{value!r} is not a default Altus knows how to resolve")


def git_origin(root: Path) -> str:
    """``owner/name`` for the checkout's origin remote, or "" if there is none.

    Empty rather than an error: a workflow with this default may still be run
    with an explicit value, and refusing here would make that impossible.
    """
    import subprocess

    try:
        found = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    if found.returncode != 0:
        return ""
    match = REMOTE.search(found.stdout.strip())
    return match.group(1) if match else ""
