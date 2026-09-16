"""Local git: the half of "do the fix, raise a PR" that happens on this machine.

The GitHub MCP server can open a pull request, comment on it and merge it. What
it cannot do is the work in between, because the work in between happens in a
checkout: edit files, make a branch, commit, push. That is what these are for.

Every one of them runs in the workspace root, so the sandbox still decides what
is reachable --- nothing here takes a path outside it.

``git_push`` is the only one that leaves the machine, and it is the reason this
module builds a ``CloudTarget``. A push to ``main`` is matched by the same
``ProtectionRules`` that guard a production cluster, through the same
``*prod*`` patterns, with no new mechanism and no second list of protected
things to keep in step.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from altus.cloud.base import CloudTarget, ProtectionMode, Sensitivity
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome

TIMEOUT = 30.0
MAX_OUTPUT = 20_000

#: Branches nobody should push to without being asked twice, on top of whatever
#: `[cloud.protected]` patterns match. These are conventions rather than
#: configuration, and a default that has to be configured to work protects
#: nobody on their first day.
DEFAULT_BRANCHES = frozenset({"main", "master", "trunk", "develop", "release"})


async def run_git(*args: str, cwd: Path) -> tuple[int, str]:
    """git, with stderr folded in --- its failures are the interesting part."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except (OSError, TimeoutError) as exc:
        return 1, f"git could not be run: {exc}"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


class GitTool(BaseTool):
    """Shared plumbing: run git in the workspace, refuse clearly outside a repo."""

    read_only: ClassVar[bool] = True

    async def git(self, ctx: ToolContext, *args: str) -> tuple[int, str]:
        return await run_git(*args, cwd=ctx.workspace.root)

    async def in_repo(self, ctx: ToolContext) -> str:
        """ "" when this is a repository, otherwise why it is not."""
        code, _ = await self.git(ctx, "rev-parse", "--is-inside-work-tree")
        return "" if code == 0 else f"{ctx.workspace.root} is not inside a git repository"

    async def branch(self, ctx: ToolContext) -> str:
        code, out = await self.git(ctx, "rev-parse", "--abbrev-ref", "HEAD")
        return out.strip() if code == 0 else ""

    async def remote_url(self, ctx: ToolContext, remote: str = "origin") -> str:
        code, out = await self.git(ctx, "remote", "get-url", remote)
        return out.strip() if code == 0 else ""

    def done(self, code: int, out: str, summary: str) -> ToolOutcome:
        text = out.strip()[:MAX_OUTPUT]
        if code != 0:
            return ToolOutcome.error(text or "git failed", summary="git failed")
        return ToolOutcome(content=text or summary, summary=summary)


# ------------------------------------------------------------------- reads


class GitStatusTool(GitTool):
    name: ClassVar[str] = "git_status"
    description: ClassVar[str] = (
        "The working tree: current branch, staged and unstaged changes, "
        "untracked files, and how far ahead or behind the upstream is."
    )
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        code, out = await self.git(ctx, "status", "--porcelain=v1", "--branch")
        return self.done(code, out, "clean")


class GitLogTool(GitTool):
    name: ClassVar[str] = "git_log"
    description: ClassVar[str] = "Recent commits on the current branch, one per line."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many commits. Default 20."},
            "path": {"type": "string", "description": "Only commits touching this path."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        limit = max(1, min(int(args.get("limit") or 20), 200))
        argv = ["log", f"-{limit}", "--oneline", "--no-decorate"]
        path = str(args.get("path") or "")
        if path:
            # Through the workspace, so `../../etc` is refused here exactly as
            # it would be for a read.
            argv += ["--", str(ctx.workspace.resolve(path))]
        code, out = await self.git(ctx, *argv)
        return self.done(code, out, f"{limit} commits")


class GitDiffTool(GitTool):
    name: ClassVar[str] = "git_diff"
    description: ClassVar[str] = (
        "What has changed and not yet been committed. Pass staged=true for "
        "what is staged, or ref to compare against a branch or commit."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean"},
            "ref": {"type": "string", "description": "A branch or commit to compare against."},
            "path": {"type": "string"},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        argv = ["diff"]
        if args.get("staged"):
            argv.append("--staged")
        ref = str(args.get("ref") or "")
        if ref:
            argv.append(ref)
        path = str(args.get("path") or "")
        if path:
            argv += ["--", str(ctx.workspace.resolve(path))]
        code, out = await self.git(ctx, *argv)
        return self.done(code, out, "no changes")


# --------------------------------------------------------------- mutations


class GitMutatingTool(GitTool):
    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "change"

    def target(self, branch: str, remote: str = "") -> CloudTarget:
        return CloudTarget(cloud="git", context=remote or "local", location=branch)

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        branch: str,
        path: str,
        diff: str = "",
        dry_run: str = "",
        remote: str = "",
        sensitivity: Sensitivity = Sensitivity.MUTATE,
        recoverability: str = "",
    ) -> ToolOutcome | None:
        """None means go ahead."""
        where = self.target(branch, remote)
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(where)) or branch in DEFAULT_BRANCHES
        if protected and getattr(rules, "mode", None) is ProtectionMode.DENY:
            return ToolOutcome.error(
                f"{branch} is protected and [cloud.protected] mode is 'deny', "
                "so this is refused outright.",
                summary="protected",
            )
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=path,
                target=where.render(),
                diff=diff,
                dry_run=dry_run,
                recoverability=recoverability,
                destructive=bool(remote),
                protected=protected,
                sensitivity=sensitivity,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected(f"the user declined: {self.action} {path}")
        return None


class GitBranchTool(GitMutatingTool):
    name: ClassVar[str] = "git_branch"
    action: ClassVar[str] = "switch to"
    description: ClassVar[str] = (
        "Create a branch and switch to it, or switch to one that exists. "
        "Uncommitted changes come with you, as they do with git switch."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "create": {"type": "boolean", "description": "Create it. Default true."},
            "from_ref": {"type": "string", "description": "Base it on this. Default HEAD."},
        },
        "required": ["name"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolOutcome.error("a branch needs a name", summary="bad arguments")

        current = await self.branch(ctx)
        create = args.get("create", True)
        base = str(args.get("from_ref") or "")
        refusal = await self.confirm(
            ctx,
            branch=name,
            path=name,
            dry_run=(
                f"currently on {current or 'an unnamed commit'}.\n"
                + (f"would create {name}" if create else f"would switch to {name}")
                + (f" from {base}" if base else "")
                + "\nUncommitted changes travel with you, as they do with git switch."
            ),
            recoverability=f"git switch {current} goes back" if current else "",
        )
        if refusal is not None:
            return refusal

        argv = ["switch"] + (["-c"] if create else []) + [name] + ([base] if base else [])
        code, out = await self.git(ctx, *argv)
        return self.done(code, out, f"on {name}")


class GitCommitTool(GitMutatingTool):
    name: ClassVar[str] = "git_commit"
    action: ClassVar[str] = "commit"
    description: ClassVar[str] = (
        "Stage paths and commit them. Shows the staged diff before asking. "
        "Never commits anything you did not name."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths to stage, relative to the workspace root.",
            },
        },
        "required": ["message", "paths"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        message = str(args.get("message") or "").strip()
        paths = [str(p) for p in (args.get("paths") or []) if str(p).strip()]
        if not message or not paths:
            return ToolOutcome.error(
                "a commit needs a message and at least one path. `paths` is "
                "required rather than defaulting to everything: committing "
                "whatever happened to be in the tree is how unrelated work "
                "ends up in somebody's fix.",
                summary="bad arguments",
            )

        # Resolved through the workspace, so nothing outside it can be staged.
        resolved = [str(ctx.workspace.resolve(p)) for p in paths]
        code, out = await self.git(ctx, "add", "--", *resolved)
        if code != 0:
            return self.done(code, out, "add failed")
        _, diff = await self.git(ctx, "diff", "--staged", "--stat")
        _, full = await self.git(ctx, "diff", "--staged")

        branch = await self.branch(ctx)
        refusal = await self.confirm(
            ctx,
            branch=branch,
            path=", ".join(paths),
            diff=full[:MAX_OUTPUT],
            dry_run=f"on branch {branch}\n{diff.strip()}",
            recoverability="git reset --soft HEAD~1 undoes it, locally",
        )
        if refusal is not None:
            # Staged and then declined. Unstaging would throw away whatever the
            # user had already staged themselves, so it is left as it is and
            # said out loud rather than quietly reverted.
            return ToolOutcome.rejected(
                f"the user declined the commit. {len(paths)} path(s) are still staged."
            )

        code, out = await self.git(ctx, "commit", "-m", message)
        return self.done(code, out, "committed")


class GitPushTool(GitMutatingTool):
    name: ClassVar[str] = "git_push"
    action: ClassVar[str] = "push"
    description: ClassVar[str] = (
        "Push the current branch to a remote. The one git tool whose effect "
        "leaves this machine, so it always asks and a protected branch needs "
        "the branch name typed."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "remote": {"type": "string", "description": "Default origin."},
            "set_upstream": {"type": "boolean", "description": "-u, for a new branch."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        problem = await self.in_repo(ctx)
        if problem:
            return ToolOutcome.error(problem, summary="no repository")
        remote = str(args.get("remote") or "origin")
        branch = await self.branch(ctx)
        if not branch or branch == "HEAD":
            return ToolOutcome.error(
                "not on a branch, so there is nothing to push", summary="detached"
            )

        url = await self.remote_url(ctx, remote)
        if not url:
            return ToolOutcome.error(f"no remote called {remote!r}", summary="no remote")
        _, ahead = await self.git(ctx, "log", "--oneline", f"{remote}/{branch}..HEAD")
        outgoing = ahead.strip() or "nothing local that the remote does not have"

        refusal = await self.confirm(
            ctx,
            branch=branch,
            remote=remote,
            path=f"{remote} {branch}",
            dry_run=(
                f"{url}\ncommits that would be pushed:\n{outgoing}\n\n"
                "Not a dry run: git has no way to ask a remote what a push "
                "would do without doing it."
            ),
            recoverability="a pushed commit is public; undoing it needs a force-push",
            sensitivity=Sensitivity.MUTATE,
        )
        if refusal is not None:
            return refusal

        argv = ["push"] + (["-u"] if args.get("set_upstream") else []) + [remote, branch]
        code, out = await self.git(ctx, *argv)
        return self.done(code, out, f"pushed {branch}")


def git_tools(*, writes: bool = True) -> list[BaseTool]:
    tools: list[BaseTool] = [GitStatusTool(), GitLogTool(), GitDiffTool()]
    if writes:
        tools += [GitBranchTool(), GitCommitTool(), GitPushTool()]
    return tools
