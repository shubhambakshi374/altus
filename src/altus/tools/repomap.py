"""The repo map as tools: ``repo_map`` and ``repo_symbol``.

Both are read-only, both answer from the index rather than from the tree, and
both bring the index up to date before answering. That last part is a choice
worth stating: a *stale* symbol table is worse than no symbol table, because
every line number in it is an invitation to read the wrong line. The
reconcile pass is built to make this affordable --- it stats the tree and
re-parses only what changed, which on this repository is 38ms against 380ms
for a cold build --- so correctness is cheap enough to take every time.

What these replace is a sequence, not a single call. The model's first moves
on an unfamiliar repository are ``list_dir``, ``glob **/*.py``, then three
``read_file`` calls to find out where things live --- four or five round trips
and a few thousand tokens of paths, to learn facts that are identical every
session. ``repo_map`` is one call that answers all of it, and ``repo_symbol``
is the one grep answers badly: grep finds every *mention* of a name, and the
question is almost always where it is *defined*.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from altus.core.errors import PathNotAllowed
from altus.repomap import churn, digest
from altus.repomap.index import MAX_FILE_BYTES, MAX_FILES, reconcile
from altus.repomap.languages import describe_absence
from altus.repomap.models import KINDS, IndexStats
from altus.repomap.store import MapStore, store_for
from altus.tools.base import BaseTool, ToolContext, ToolOutcome


async def _answer(ctx: ToolContext, render: Callable[[MapStore], str]) -> str:
    """Refresh the index and render an answer from it, all on one thread.

    The single-thread rule is not a style preference: a SQLite connection
    belongs to the thread that opened it, so the store cannot be built inside
    ``to_thread`` and then queried on the event loop. Passing ``render`` in,
    rather than passing the store out, is what makes that impossible to get
    wrong --- the connection never leaves the worker.

    It is also why ``churn`` is synchronous. Everything in this pass is
    blocking work with a thread already allocated to it, and an async git call
    would have to run on the loop, which is the side of the boundary the store
    cannot be used from.
    """
    settings = getattr(ctx, "repo_map_settings", None)
    max_files = int(getattr(settings, "max_files", MAX_FILES) or MAX_FILES)
    max_bytes = int(getattr(settings, "max_file_bytes", MAX_FILE_BYTES) or MAX_FILE_BYTES)
    commits = int(getattr(settings, "churn_commits", churn.COMMITS) or 0)

    def _work() -> str:
        db = store_for(ctx.workspace.root)
        try:
            stats = reconcile(
                ctx.workspace, store=db, max_files=max_files, max_file_bytes=max_bytes
            )
            if commits > 0:
                # The optional half. A directory that is not a checkout still
                # has a map, and losing the hotspots section is not worth
                # failing the call over.
                churn.update(db, ctx.workspace.root, commits=commits)
            return _joined(render(db), _note(stats))
        finally:
            db.close()

    return await asyncio.to_thread(_work)


def _note(stats: IndexStats) -> str:
    """What the caller needs told about the index itself, if anything.

    Three things qualify: a language we could not read (otherwise an empty
    answer looks like an empty repository), a truncated walk, and files that
    would not parse. Everything else about the pass is noise to a caller who
    asked what is in the repository.
    """
    parts: list[str] = []
    absence = describe_absence(stats.unsupported)
    if absence:
        parts.append(absence)
    if stats.scanned >= MAX_FILES:
        parts.append(f"the walk stopped at {stats.scanned} files, so this map is partial")
    if stats.skipped:
        parts.append(f"{stats.skipped} file(s) too large to parse")
    if stats.failed:
        shown = ", ".join(stats.failed[:3])
        more = f" and {len(stats.failed) - 3} more" if len(stats.failed) > 3 else ""
        parts.append(f"could not parse {shown}{more}")
    return "\n".join(f"note: {part}" for part in parts)


def _joined(body: str, note: str) -> str:
    return f"{body}\n\n{note}" if note else body


class RepoMapTool(BaseTool):
    name: ClassVar[str] = "repo_map"
    description: ClassVar[str] = (
        "Orient yourself in this repository from a pre-built index: its packages and what "
        "each one is for, the files the rest of the tree depends on most, and where recent "
        "commits have landed. With 'path' set to a directory, lists that directory's files "
        "and what each contains; with 'path' set to a file, lists that file's definitions "
        "with line numbers, what it imports from this repo, and what imports it. "
        "Prefer this over list_dir and glob when the question is 'how is this code "
        "organised' or 'where should I look' --- it is one call instead of several, and it "
        "answers from parsed definitions rather than from filenames."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "A file or directory, relative to the workspace root. Omit for the "
                    "whole-repository overview."
                ),
            },
            "exported_only": {
                "type": "boolean",
                "description": (
                    "For a file: show only public definitions. Useful when reading a "
                    "module as a consumer rather than a maintainer."
                ),
            },
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        target = str(args.get("path") or "").strip()
        exported_only = bool(args.get("exported_only"))

        resolved: Path | None = None
        if target:
            try:
                resolved = ctx.workspace.resolve(target)
            except PathNotAllowed as exc:
                return ToolOutcome.error(str(exc), summary="denied")

        if resolved is None:
            counted = 0

            def _overview(db: MapStore) -> str:
                nonlocal counted
                counted = db.file_count()
                return digest.overview(db)

            return ToolOutcome(
                content=await _answer(ctx, _overview),
                summary=f"map of {counted} files",
            )

        relative = ctx.workspace.relative(resolved)
        if resolved.is_dir():
            body = await _answer(ctx, lambda db: digest.package_card(db, relative))
        else:
            body = await _answer(
                ctx,
                lambda db: digest.file_card(db, relative, exported_only=exported_only),
            )
        return ToolOutcome(content=body, summary=f"map of {relative}")


class RepoSymbolTool(BaseTool):
    name: ClassVar[str] = "repo_symbol"
    description: ClassVar[str] = (
        "Find where a name is DEFINED in this repository --- classes, functions, methods, "
        "types and constants --- with its file, line, signature and docstring summary. "
        "Matches case-insensitively on a substring, so a half-remembered name works. "
        "Prefer this over grep when you want the definition: grep returns every mention, "
        "and the definition is one line among hundreds of call sites."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name, or part of it.",
            },
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "Narrow to one kind of definition.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum definitions to return. Default 25.",
            },
        },
        "required": ["name"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolOutcome.error("name is required", summary="bad arguments")
        kind = str(args.get("kind") or "").strip()
        if kind and kind not in KINDS:
            return ToolOutcome.error(
                f"unknown kind {kind!r}. Known: {', '.join(KINDS)}", summary="bad arguments"
            )
        try:
            limit = max(1, min(200, int(args.get("limit") or 25)))
        except TypeError, ValueError:
            return ToolOutcome.error("limit must be a number", summary="bad arguments")

        hits = 0

        def _card(db: MapStore) -> str:
            nonlocal hits
            hits = len(db.find(name, kind=kind, limit=limit))
            return digest.symbol_card(db, name, kind=kind, limit=limit)

        body = await _answer(ctx, _card)
        return ToolOutcome(content=body, summary=f"{hits} definition(s) of {name}")


def repo_map_tools() -> list[BaseTool]:
    """Both tools. Read-only, so there is no ``writes`` variant."""
    return [RepoMapTool(), RepoSymbolTool()]
