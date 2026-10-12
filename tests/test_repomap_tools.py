"""``repo_map`` and ``repo_symbol``: the doors the model actually uses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from altus.config.models import RepoMapSettings
from altus.tools.base import ToolContext
from altus.tools.registry import ToolRegistry
from altus.tools.repomap import repo_map_tools
from altus.workspace import Workspace

SAMPLE = '''"""Runs the engine."""

from core.shared import Shared


class Engine:
    """The engine itself."""

    def start(self) -> None:
        """Begin."""

    def _warm(self) -> None: ...


def build() -> Engine:
    """Make one."""
    return Engine()
'''


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True)
    (root / "core" / "__init__.py").write_text('"""The core."""\n')
    (root / "core" / "shared.py").write_text('"""Shared bits."""\n\n\nclass Shared: ...\n')
    (root / "core" / "engine.py").write_text(SAMPLE)
    return root


@pytest.fixture
def ctx(tree: Path) -> ToolContext:
    return ToolContext(workspace=Workspace(root=tree), repo_map_settings=RepoMapSettings())


@pytest.fixture
def tools() -> ToolRegistry:
    return ToolRegistry(repo_map_tools())


async def run(tools: ToolRegistry, name: str, args: dict[str, object], ctx: ToolContext) -> str:
    outcome = await tools.execute(name, args, ctx)
    assert not outcome.is_error, outcome.content
    return outcome.content


# -------------------------------------------------------------------- the map


async def test_the_overview_needs_no_arguments(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_map", {}, ctx)
    assert "repo: 3 files" in body
    assert "The core." in body


async def test_a_directory_lists_its_files(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_map", {"path": "core"}, ctx)
    assert "engine.py" in body
    assert "Shared bits." in body


async def test_a_file_lists_definitions_with_line_numbers(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    body = await run(tools, "repo_map", {"path": "core/engine.py"}, ctx)
    assert "Runs the engine." in body
    assert "class Engine" in body
    assert "_warm" in body
    assert "(private)" in body


async def test_exported_only_hides_the_private_definitions(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    body = await run(tools, "repo_map", {"path": "core/engine.py", "exported_only": True}, ctx)
    assert "class Engine" in body
    assert "_warm" not in body


async def test_a_file_card_names_what_imports_it(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_map", {"path": "core/shared.py"}, ctx)
    assert "imported by 1" in body
    assert "core/engine.py" in body


async def test_a_path_outside_the_workspace_is_refused(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    outcome = await tools.execute("repo_map", {"path": "../../etc"}, ctx)
    assert outcome.is_error
    assert outcome.summary == "denied"


async def test_an_unindexed_path_is_explained_not_empty(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    (ctx.workspace.root / "notes.md").write_text("# notes\n")
    body = await run(tools, "repo_map", {"path": "notes.md"}, ctx)
    assert "not in the map" in body


# ----------------------------------------------------------------- the symbol


async def test_a_symbol_is_found_by_exact_name(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_symbol", {"name": "Engine"}, ctx)
    assert "core/engine.py:6" in body
    assert "class Engine" in body
    assert "The engine itself." in body


async def test_a_partial_name_matches(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_symbol", {"name": "engi"}, ctx)
    assert "Engine" in body


async def test_a_kind_narrows_the_search(tools: ToolRegistry, ctx: ToolContext) -> None:
    assert "class Engine" in await run(tools, "repo_symbol", {"name": "Engine"}, ctx)
    narrowed = await run(tools, "repo_symbol", {"name": "Engine", "kind": "method"}, ctx)
    assert "no definition matching" in narrowed


async def test_an_unknown_kind_is_refused_by_name(tools: ToolRegistry, ctx: ToolContext) -> None:
    outcome = await tools.execute("repo_symbol", {"name": "x", "kind": "struct"}, ctx)
    assert outcome.is_error
    assert "unknown kind" in outcome.content


async def test_a_missing_name_is_refused(tools: ToolRegistry, ctx: ToolContext) -> None:
    outcome = await tools.execute("repo_symbol", {}, ctx)
    assert outcome.is_error
    assert "name is required" in outcome.content


async def test_nothing_found_says_so(tools: ToolRegistry, ctx: ToolContext) -> None:
    body = await run(tools, "repo_symbol", {"name": "Nonexistent"}, ctx)
    assert "no definition matching" in body


# ----------------------------------------------------------------- freshness


async def test_a_new_definition_is_visible_on_the_next_call(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    """The map is reconciled before it answers. A stale symbol table is worse
    than none, because its line numbers all look plausible."""
    assert "no definition matching" in await run(tools, "repo_symbol", {"name": "Later"}, ctx)

    (ctx.workspace.root / "core" / "later.py").write_text("class Later: ...\n")
    body = await run(tools, "repo_symbol", {"name": "Later"}, ctx)
    assert "core/later.py:1" in body


async def test_a_moved_definition_reports_its_new_line(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    target = ctx.workspace.root / "core" / "engine.py"
    await run(tools, "repo_symbol", {"name": "Engine"}, ctx)

    target.write_text("# a new first line\n" * 4 + SAMPLE)
    body = await run(tools, "repo_symbol", {"name": "Engine"}, ctx)
    assert "core/engine.py:10" in body


async def test_a_deleted_file_stops_being_an_answer(tools: ToolRegistry, ctx: ToolContext) -> None:
    await run(tools, "repo_symbol", {"name": "Shared"}, ctx)
    (ctx.workspace.root / "core" / "shared.py").unlink()
    assert "no definition matching" in await run(tools, "repo_symbol", {"name": "Shared"}, ctx)


# --------------------------------------------------------------------- notes


async def test_an_unparseable_language_is_noted_on_the_answer(
    tools: ToolRegistry, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a Go repository indexed without the extra reads as a Go
    repository containing nothing."""
    from altus.repomap.languages import PythonExtractor

    (ctx.workspace.root / "main.go").write_text("package main\n\nfunc Hello() {}\n")
    monkeypatch.setattr(
        "altus.repomap.index.extractor_for",
        lambda language: PythonExtractor() if language == "python" else None,
    )
    body = await run(tools, "repo_map", {}, ctx)
    assert "note: not indexed: 1 go" in body
    assert "altus[repomap]" in body


async def test_a_file_that_will_not_parse_is_noted(tools: ToolRegistry, ctx: ToolContext) -> None:
    (ctx.workspace.root / "core" / "broken.py").write_text("def (((:\n")
    body = await run(tools, "repo_map", {}, ctx)
    assert "could not parse core/broken.py" in body


async def test_an_oversized_file_is_counted_in_a_note(
    tools: ToolRegistry, ctx: ToolContext
) -> None:
    (ctx.workspace.root / "core" / "huge.py").write_text("X = 1\n" * 5000)
    ctx.repo_map_settings = RepoMapSettings(max_file_bytes=1024)
    body = await run(tools, "repo_map", {}, ctx)
    assert "too large to parse" in body


async def test_both_tools_are_reads(tools: ToolRegistry) -> None:
    from altus.cloud.base import Sensitivity
    from altus.tools.base import sensitivity_of

    for tool in tools:
        assert tool.read_only
        assert sensitivity_of(tool) is Sensitivity.READ


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """A committed checkout. Built in a sync fixture because an async test
    may not block on a subprocess."""
    root = tmp_path / "hist"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    for key, value in (("user.email", "a@b.c"), ("user.name", "A")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "busy.py").write_text("A = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "one"], check=True)
    return root


async def test_hotspots_appear_once_there_is_history(history: Path) -> None:
    ctx = ToolContext(workspace=Workspace(root=history), repo_map_settings=RepoMapSettings())
    body = await run(ToolRegistry(repo_map_tools()), "repo_map", {}, ctx)
    assert "Where the work is" in body
    assert "busy.py" in body


async def test_churn_can_be_switched_off(history: Path) -> None:
    ctx = ToolContext(
        workspace=Workspace(root=history),
        repo_map_settings=RepoMapSettings(churn_commits=0),
    )
    body = await run(ToolRegistry(repo_map_tools()), "repo_map", {}, ctx)
    assert "Where the work is" not in body
