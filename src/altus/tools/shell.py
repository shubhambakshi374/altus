"""Running a command: the half of a software factory that builds and tests.

The workflow engine can edit a file, commit it and open a pull request. What it
could not do until now is find out whether the thing it changed still works,
because ``make``, ``pytest`` and ``npm ci`` had no door. This is that door, and
it is deliberately a *tool* rather than a new kind of step: a shell step is a
``ToolStep`` calling ``shell_run``, so it inherits validation, the approval
gate, the run record and the blast radius with no new mechanism.

Two properties hold it up.

**A command is an argv list handed to execve, never a string handed to a
shell.** There is no ``sh -c`` in this module, exactly as there is none in
``tools/cli.py``. An argument containing ``; rm -rf /`` arrives at ``make`` as
one literal argument and does nothing.

**The allowlist is a list, not a prompt.** A binary that is not in
``[tools.shell] allow`` is refused outright rather than gated, and with an
empty allowlist the tool is never registered at all. The gate underneath exists
to ask what *this* ``make`` invocation will do; it is not a second chance to
decide whether ``curl`` was meant.

What this does not do is claim to know the answer in advance. Every call
classifies as ``MUTATE`` with ``dispatches = True``, so a workflow containing
one always reads ``(at least)``. There is no read/write subcommand table here
like ``cli.py`` keeps for kubectl: ``pytest`` writes files, ``make`` runs
whatever a Makefile says, and a table claiming otherwise would be the gate
naming a check that never ran.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, ClassVar

from altus.cloud.base import Sensitivity
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome

MAX_OUTPUT = 40_000
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0

NO_PREVIEW = "Not a dry run: there is no way to ask a command what it would do without running it."


class ShellTool(BaseTool):
    """One allowlisted binary, with its arguments, inside the workspace."""

    name: ClassVar[str] = "shell_run"
    read_only: ClassVar[bool] = False
    dispatches: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Run an allowlisted command --- a build, a test run, a linter. "
        "Arguments are a list, one per element, and are never interpreted by a "
        "shell: pipes, redirection and semicolons are literal text. The command "
        "runs inside the workspace and always asks before it runs."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("The binary and its arguments, one per element. Not a shell line."),
            },
            "cwd": {
                "type": "string",
                "description": "Directory to run in, relative to the workspace root.",
            },
            "timeout": {
                "type": "number",
                "description": "Seconds before the command is cut off.",
            },
            "reason": {
                "type": "string",
                "description": "What this is for. Shown to the user in the prompt.",
            },
        },
        "required": ["argv"],
    }

    def allowlist(self, ctx: ToolContext) -> tuple[str, ...]:
        """Checked at call time as well as at registration.

        ``cli.py`` learned this the hard way: its allowlist check read an
        attribute that never existed, so for a while the list was enforced only
        by registration. A tool must not depend on having been registered
        correctly in order to be safe.
        """
        return tuple(getattr(ctx.shell_settings, "allow", ()) or ())

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        raw = args.get("argv")
        if not isinstance(raw, list) or not raw or not all(isinstance(a, str) for a in raw):
            return ToolOutcome.error(
                "argv must be a non-empty array of strings, one argument per "
                "element. There is no shell here, so pipes and redirection do "
                "not work --- run the pieces separately.",
                summary="bad arguments",
            )
        argv = [str(a) for a in raw]
        binary = argv[0]

        allowed = self.allowlist(ctx)
        if binary not in allowed:
            listed = ", ".join(sorted(allowed)) if allowed else "(nothing)"
            return ToolOutcome.error(
                f"{binary!r} is not in [tools.shell] allow, which lists: {listed}. "
                "This is refused rather than asked about --- adding a binary is "
                "a decision for the config file, not for a prompt.",
                summary="not allowed",
            )
        if "/" in binary or "\\" in binary:
            # An allowlist of names cannot vouch for a path. `./make` is not
            # `make`, and matching it against the list would be the check
            # agreeing with itself rather than with the config.
            return ToolOutcome.error(
                "the command must be a bare binary name, not a path", summary="bad arguments"
            )
        if shutil.which(binary) is None:
            return ToolOutcome.error(f"{binary} is not installed", summary="not installed")

        try:
            cwd = ctx.workspace.resolve(str(args.get("cwd") or "."))
        except Exception as exc:
            # Outside the sandbox is an error, not a prompt: the workspace
            # decides what is reachable and a gate cannot overrule it.
            return ToolOutcome.error(str(exc), summary="outside the workspace")
        if not cwd.is_dir():
            return ToolOutcome.error(f"{cwd} is not a directory", summary="no such directory")

        timeout = self.timeout(args, ctx)
        printed = " ".join(argv)
        reason = str(args.get("reason") or "").strip()
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action="run",
                path=printed,
                target=f"shell: {cwd}",
                dry_run=(
                    (f"{reason}\n\n" if reason else "")
                    + f"{printed}\nin {cwd}, giving up after {timeout:g}s\n\n"
                    + NO_PREVIEW
                ),
                recoverability=(
                    "whatever this writes to the working tree, git can show you "
                    "afterwards; whatever it sends elsewhere, it cannot"
                ),
                sensitivity=Sensitivity.MUTATE,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected(f"the user declined: {printed}")

        code, out = await run_command(argv, cwd=cwd, seconds=timeout)
        text = out.strip()[:MAX_OUTPUT]
        if code != 0:
            return ToolOutcome.error(text or f"{binary} exited {code}", summary=f"exit {code}")
        return ToolOutcome(content=text or "(no output)", summary="ok")

    def timeout(self, args: dict[str, Any], ctx: ToolContext) -> float:
        configured = float(
            getattr(ctx.shell_settings, "timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT
        )
        asked = args.get("timeout")
        wanted = float(asked) if isinstance(asked, int | float) else configured
        return max(1.0, min(wanted, MAX_TIMEOUT))


async def run_command(argv: list[str], *, cwd: Path, seconds: float) -> tuple[int, str]:
    """Run it, stderr folded in --- a failing build says why on stderr.

    A timeout kills the process and returns what it printed so far, which is
    the half of a hung test run worth having.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 1, f"{argv[0]} could not be run: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, f"{argv[0]} was still running after {seconds:g}s and was stopped"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")
