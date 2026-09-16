"""Tool behaviour: limits, refusals, and gitignore handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from altus.tools import ToolContext, default_registry
from altus.tools.fs import GrepTool
from altus.workspace import Workspace


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".gitignore").write_text(".venv/\n*.log\nbuild/\n")
    (root / "src" / "main.py").write_text("def hello():\n    return 'world'\n")
    (root / "src" / "pkg" / "util.py").write_text("VALUE = 42\nhello = 1\n")
    (root / ".venv" / "lib" / "noise.py").write_text("hello = 'ignored'\n")
    (root / ".git" / "config").write_text("hello = 'git'\n")
    (root / "debug.log").write_text("hello log\n")
    (root / "README.md").write_text("# Repo\n")
    return root


@pytest.fixture
def ctx(tree: Path) -> ToolContext:
    return ToolContext(workspace=Workspace(root=tree))


@pytest.fixture
def registry():  # type: ignore[no-untyped-def]
    # Filesystem tools only: the cloud sets have their own test modules.
    return default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)


async def run(registry, name, args, ctx):  # type: ignore[no-untyped-def]
    return await registry.execute(name, args, ctx)


# ------------------------------------------------------------------- read_file


async def test_read_file_is_line_numbered(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "read_file", {"path": "src/main.py"}, ctx)
    assert not out.is_error
    assert "1\tdef hello():" in out.content
    assert "2\t    return 'world'" in out.content


async def test_read_file_offset_and_limit(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "long.txt").write_text("\n".join(f"line{i}" for i in range(1, 101)) + "\n")
    out = await run(registry, "read_file", {"path": "long.txt", "offset": 50, "limit": 3}, ctx)
    assert "50\tline50" in out.content
    assert "52\tline52" in out.content
    assert "line53" not in out.content
    assert "offset=53" in out.content


async def test_read_file_truncates_at_max_lines(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "long.txt").write_text("\n".join(f"line{i}" for i in range(1, 51)) + "\n")
    small = ToolContext(workspace=ctx.workspace, max_lines=10)
    out = await run(registry, "read_file", {"path": "long.txt"}, small)
    assert "10\tline10" in out.content
    assert "line11" not in out.content
    assert "continue with offset=11" in out.content


async def test_read_file_truncates_at_max_bytes(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "big.txt").write_text("x" * 5000 + "\n")
    small = ToolContext(workspace=ctx.workspace, max_file_bytes=100)
    out = await run(registry, "read_file", {"path": "big.txt"}, small)
    assert "exceeds" in out.content
    assert len(out.content) < 500


async def test_read_file_refuses_binary(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "blob.bin").write_bytes(b"\x89PNG\x00\x01\x02binary")
    out = await run(registry, "read_file", {"path": "blob.bin"}, ctx)
    assert out.is_error
    assert "binary" in out.content


async def test_read_file_reports_empty(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "empty.txt").write_text("")
    out = await run(registry, "read_file", {"path": "empty.txt"}, ctx)
    assert not out.is_error
    assert "empty" in out.content


async def test_read_file_missing(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "read_file", {"path": "nope.py"}, ctx)
    assert out.is_error and "no such file" in out.content


async def test_read_file_on_a_directory_redirects(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "read_file", {"path": "src"}, ctx)
    assert out.is_error and "list_dir" in out.content


async def test_read_file_offset_past_end(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "read_file", {"path": "src/main.py", "offset": 999}, ctx)
    assert out.is_error and "past the end" in out.content


async def test_read_file_denies_escape_and_secrets(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    for path in ("/etc/passwd", "../../etc/passwd"):
        out = await run(registry, "read_file", {"path": path}, ctx)
        assert out.is_error and out.summary == "denied"


async def test_read_file_denies_dotenv(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / ".env").write_text("SECRET=1\n")
    out = await run(registry, "read_file", {"path": ".env"}, ctx)
    assert out.is_error and out.summary == "denied"
    assert "SECRET" not in out.content


async def test_read_file_requires_a_path(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "read_file", {}, ctx)
    assert out.is_error and "path is required" in out.content


# -------------------------------------------------------------------- list_dir


async def test_list_dir_hides_gitignored_and_git(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "list_dir", {}, ctx)
    assert "src/" in out.content
    assert "README.md" in out.content
    assert ".venv" not in out.content
    assert ".git/" not in out.content
    assert "debug.log" not in out.content
    assert "hidden by .gitignore" in out.content


async def test_list_dir_all_includes_ignored_but_never_git(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "list_dir", {"all": True}, ctx)
    assert ".venv/" in out.content
    assert "debug.log" in out.content
    assert ".git/" not in out.content, ".git is always skipped"


async def test_list_dir_directories_before_files(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "list_dir", {}, ctx)
    lines = [ln.strip() for ln in out.content.splitlines()[1:] if ln.strip()]
    dir_rows = [i for i, ln in enumerate(lines) if ln.endswith("/")]
    file_rows = [i for i, ln in enumerate(lines) if "(" in ln]
    assert max(dir_rows) < min(file_rows)


async def test_list_dir_caps_entries(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    big = tree / "many"
    big.mkdir()
    for i in range(30):
        (big / f"f{i:03}.txt").write_text("x")
    small = ToolContext(workspace=ctx.workspace, max_entries=5)
    out = await run(registry, "list_dir", {"path": "many"}, small)
    assert "showing 5 of 30 entries" in out.content


async def test_list_dir_on_a_file_redirects(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "list_dir", {"path": "README.md"}, ctx)
    assert out.is_error and "read_file" in out.content


async def test_list_dir_denies_escape(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "list_dir", {"path": "/etc"}, ctx)
    assert out.is_error and out.summary == "denied"


# ------------------------------------------------------------------------ glob


async def test_glob_excludes_ignored_and_git(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "glob", {"pattern": "**/*.py"}, ctx)
    assert "src/main.py" in out.content
    assert "src/pkg/util.py" in out.content
    assert ".venv" not in out.content


async def test_glob_no_matches_is_not_an_error(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "glob", {"pattern": "**/*.rs"}, ctx)
    assert not out.is_error
    assert "no files match" in out.content


async def test_glob_sorts_newest_first(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    import os

    (tree / "old.py").write_text("old\n")
    (tree / "new.py").write_text("new\n")
    os.utime(tree / "old.py", (1, 1))
    os.utime(tree / "new.py", (10_000_000, 10_000_000))
    out = await run(registry, "glob", {"pattern": "*.py"}, ctx)
    lines = out.content.splitlines()
    assert lines.index("new.py") < lines.index("old.py")


async def test_glob_skips_denylisted_secrets(registry, ctx, tree) -> None:  # type: ignore[no-untyped-def]
    (tree / "server.pem").write_text("KEY\n")
    out = await run(registry, "glob", {"pattern": "*.pem"}, ctx)
    assert "server.pem" not in out.content


async def test_glob_requires_a_pattern(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "glob", {}, ctx)
    assert out.is_error


# ------------------------------------------------------------------------ grep


async def test_grep_finds_matches_with_path_and_line(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "grep", {"pattern": "hello"}, ctx)
    assert not out.is_error
    assert "src/main.py:1:" in out.content


async def test_grep_excludes_ignored_and_git(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "grep", {"pattern": "hello"}, ctx)
    assert ".venv" not in out.content
    assert ".git/config" not in out.content
    assert "debug.log" not in out.content


async def test_grep_python_fallback_matches_ripgrep(registry, ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The fallback must work identically when rg is not installed."""
    with_rg = await run(registry, "grep", {"pattern": "hello"}, ctx)

    monkeypatch.setattr(GrepTool, "use_ripgrep", False)
    without_rg = await run(registry, "grep", {"pattern": "hello"}, ctx)

    assert not without_rg.is_error
    assert "src/main.py:1:" in without_rg.content
    assert set(with_rg.content.splitlines()) == set(without_rg.content.splitlines())


async def test_grep_fallback_excludes_ignored(registry, ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(GrepTool, "use_ripgrep", False)
    out = await run(registry, "grep", {"pattern": "hello"}, ctx)
    assert ".venv" not in out.content
    assert ".git" not in out.content


async def test_grep_fallback_skips_binary(registry, ctx, tree, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tree / "blob.bin").write_bytes(b"\x00hello binary\n")
    monkeypatch.setattr(GrepTool, "use_ripgrep", False)
    out = await run(registry, "grep", {"pattern": "hello"}, ctx)
    assert "blob.bin" not in out.content


async def test_grep_invalid_regex_is_a_clean_error(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "grep", {"pattern": "([unclosed"}, ctx)
    assert out.is_error and "invalid regular expression" in out.content


async def test_grep_no_matches_is_not_an_error(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "grep", {"pattern": "zzz_not_present"}, ctx)
    assert not out.is_error and "no matches" in out.content


async def test_grep_denies_escape(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "grep", {"pattern": "root", "path": "/etc"}, ctx)
    assert out.is_error and out.summary == "denied"


# -------------------------------------------------------------------- registry


def test_registry_splits_read_only_from_mutating(registry) -> None:  # type: ignore[no-untyped-def]
    assert registry.names == [
        "delete_path",
        "edit_file",
        "glob",
        "grep",
        "list_dir",
        "read_file",
        "write_file",
    ]
    assert sorted(t.name for t in registry if t.read_only) == [
        "glob",
        "grep",
        "list_dir",
        "read_file",
    ]
    assert sorted(t.name for t in registry if not t.read_only) == [
        "delete_path",
        "edit_file",
        "write_file",
    ]


def test_registry_can_omit_the_write_tools() -> None:
    from altus.tools import default_registry as make

    assert make(
        writes=False, kubernetes=False, aws=False, azure=False, gcp=False, mcp=False
    ).names == [
        "glob",
        "grep",
        "list_dir",
        "read_file",
    ]


def test_kubernetes_tools_register_only_when_the_sdk_is_present() -> None:
    from altus.tools import default_registry as make

    with_k8s = make(kubernetes=True, aws=False, azure=False, gcp=False).names
    without = make(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False).names
    assert any(n.startswith("k8s_") for n in with_k8s)
    assert not any(n.startswith("k8s_") for n in without)


def test_aws_tools_register_without_an_extra() -> None:
    """boto3 is a core dependency because bedrock needs it, so unlike the
    Kubernetes set there is nothing to install."""
    from altus.tools import default_registry as make

    assert any(n.startswith("aws_") for n in make(kubernetes=False, aws=True).names)
    assert not any(
        n.startswith("aws_")
        for n in make(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False).names
    )


def test_tool_defs_are_stable_and_schema_shaped(registry) -> None:  # type: ignore[no-untyped-def]
    defs = registry.to_tool_defs()
    assert [d.name for d in defs] == registry.names
    for d in defs:
        assert d.description
        assert d.input_schema["type"] == "object"
        assert "properties" in d.input_schema


async def test_unknown_tool_is_an_outcome_not_an_exception(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await run(registry, "rm_rf", {}, ctx)
    assert out.is_error and "unknown tool" in out.content


async def test_tool_exceptions_become_error_outcomes(registry, ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A raised exception would abort a turn the model could recover from."""

    async def boom(self, args, c):  # type: ignore[no-untyped-def]
        raise OSError("disk on fire")

    monkeypatch.setattr("altus.tools.fs.ReadFileTool.run", boom)
    out = await run(registry, "read_file", {"path": "src/main.py"}, ctx)
    assert out.is_error and "disk on fire" in out.content


async def test_non_dict_arguments_are_rejected(registry, ctx) -> None:  # type: ignore[no-untyped-def]
    out = await registry.execute("read_file", ["oops"], ctx)  # type: ignore[arg-type]
    assert out.is_error and "must be an object" in out.content


def test_writes_off_means_no_writes_anywhere() -> None:
    """It used to filter only the filesystem tools, so a read-only registry
    still carried k8s_delete and aws_write --- and build_system_prompt then
    told the model it had read-only access while handing it a drain."""
    from altus.tools import default_registry as make

    registry = make(writes=False, kubernetes=True, aws=True)
    mutating = [name for name in registry.names if not registry.is_read_only(name)]
    assert mutating == [], f"writes=False still registered: {mutating}"
    assert len(registry.names) > 10, "and the reads are all still there"
