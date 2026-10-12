"""The index: what it extracts, what it refuses to re-parse, and what it forgets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from altus.repomap.index import SKIP_DIRS, reconcile
from altus.repomap.languages import (
    PythonExtractor,
    available_languages,
    describe_absence,
    language_of,
    missing_languages,
)
from altus.repomap.store import MapStore, map_path, store_for
from altus.workspace import Workspace

SAMPLE = '''"""A module that does one thing.

The rest of the docstring is not the summary and must not be stored.
"""

from __future__ import annotations

import os
from pathlib import Path

from altus.core.errors import ToolError
from . import sibling
from ..parent import thing

MAX_TRIES = 3
Some_Constant = "kept"
lowercase_variable = "dropped"


class Engine:
    """Runs the thing."""

    def start(self) -> None:
        """Begin."""

    async def _private(self, count: int = 3) -> str:
        return ""


def build(name: str, *, strict: bool = False) -> Engine:
    """Make one."""

    def nested() -> None:
        """Not a top-level definition."""

    return Engine()


async def _hidden() -> None: ...
'''


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text('"""The package."""\n')
    (root / "src" / "pkg" / "engine.py").write_text(SAMPLE)
    return root


@pytest.fixture
def workspace(tree: Path) -> Workspace:
    return Workspace(root=tree)


def test_language_is_decided_by_extension() -> None:
    assert language_of("a/b/c.py") == "python"
    assert language_of("main.go") == "go"
    assert language_of("app.tsx") == "tsx"
    assert language_of("README.md") == ""
    assert language_of("Makefile") == ""


def test_python_is_always_available_without_the_extra() -> None:
    assert "python" in available_languages()
    assert "python" not in missing_languages()


# ------------------------------------------------------------------ extraction


def test_extracts_definitions_but_not_nested_ones() -> None:
    found = PythonExtractor().extract(SAMPLE, "src/pkg/engine.py")
    names = {(s.name, s.kind, s.parent) for s in found.symbols}
    assert ("Engine", "class", "") in names
    assert ("start", "method", "Engine") in names
    assert ("build", "function", "") in names
    assert "nested" not in {s.name for s in found.symbols}


def test_stores_only_the_summary_line_of_a_docstring() -> None:
    found = PythonExtractor().extract(SAMPLE, "x.py")
    assert found.doc == "A module that does one thing."
    assert "must not be stored" not in found.doc


def test_signatures_carry_arguments_and_return_types() -> None:
    found = PythonExtractor().extract(SAMPLE, "x.py")
    build = next(s for s in found.symbols if s.name == "build")
    assert build.signature == "def build(name: str, *, strict: bool=False) -> Engine"
    private = next(s for s in found.symbols if s.name == "_private")
    assert private.signature.startswith("async def _private(")


def test_underscore_decides_exported_when_there_is_no_dunder_all() -> None:
    found = PythonExtractor().extract(SAMPLE, "x.py")
    exported = {s.name for s in found.symbols if s.exported}
    assert "build" in exported
    assert "_hidden" not in exported
    assert "_private" not in exported


def test_dunder_all_overrides_the_underscore_convention() -> None:
    text = '__all__ = ["_wanted"]\n\ndef _wanted() -> None: ...\n\ndef public() -> None: ...\n'
    found = PythonExtractor().extract(text, "x.py")
    exported = {s.name for s in found.symbols if s.exported}
    assert exported == {"_wanted"}


def test_only_constant_shaped_assignments_are_recorded() -> None:
    found = PythonExtractor().extract(SAMPLE, "x.py")
    constants = {s.name for s in found.symbols if s.kind == "constant"}
    assert constants == {"MAX_TRIES", "Some_Constant"}


def test_imports_under_type_checking_are_still_edges() -> None:
    text = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from altus.core.types import Message\n"
    )
    found = PythonExtractor().extract(text, "x.py")
    assert "altus.core.types" in {imp.target for imp in found.imports}


def test_relative_imports_keep_their_dots() -> None:
    found = PythonExtractor().extract(SAMPLE, "src/pkg/engine.py")
    targets = {imp.target for imp in found.imports}
    assert "." in targets
    assert "..parent" in targets


# ------------------------------------------------------------------- the walk


def test_reconcile_indexes_the_tree(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        stats = reconcile(workspace, store=db)
        assert stats.parsed == 2
        assert stats.scanned == 2
        assert db.file_count() == 2
        assert db.languages() == [("python", 2)]
        assert db.find("Engine", exact=True)[0][0] == "src/pkg/engine.py"


def test_a_second_pass_parses_nothing(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        again = reconcile(workspace, store=db)
        assert again.parsed == 0
        assert again.unchanged == 2


def test_identical_bytes_with_a_new_mtime_are_not_reparsed(workspace: Workspace) -> None:
    target = workspace.root / "src" / "pkg" / "engine.py"
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        stat = target.stat()
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        touched = reconcile(workspace, store=db)
        assert touched.parsed == 0

        # And the gate has to close again, or every later pass re-hashes it.
        settled = reconcile(workspace, store=db)
        assert settled.parsed == 0
        assert settled.unchanged == 2


def test_changed_content_is_reparsed_and_old_symbols_go(workspace: Workspace) -> None:
    target = workspace.root / "src" / "pkg" / "engine.py"
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        assert db.find("Engine", exact=True)

        target.write_text("def only_this() -> None: ...\n")
        stats = reconcile(workspace, store=db)

        assert stats.parsed == 1
        assert not db.find("Engine", exact=True)
        assert db.find("only_this", exact=True)


def test_a_deleted_file_is_forgotten(workspace: Workspace) -> None:
    target = workspace.root / "src" / "pkg" / "engine.py"
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        target.unlink()
        stats = reconcile(workspace, store=db)

        assert stats.removed == 1
        assert db.file_count() == 1
        assert not db.find("Engine", exact=True)


def test_full_reparses_everything(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        stats = reconcile(workspace, store=db, full=True)
        assert stats.parsed == 2
        assert stats.unchanged == 0


def test_a_file_that_does_not_parse_is_recorded_and_the_walk_continues(
    workspace: Workspace,
) -> None:
    (workspace.root / "src" / "pkg" / "broken.py").write_text("def (((:\n")
    with store_for(workspace.root) as db:
        stats = reconcile(workspace, store=db)

        assert stats.failed == ["src/pkg/broken.py"]
        assert stats.parsed == 2
        # Recorded, not skipped: otherwise it is re-read on every later pass.
        assert db.file_count() == 3
        assert db.symbols_in("src/pkg/broken.py") == []


def test_files_over_the_cap_are_counted_not_parsed(workspace: Workspace) -> None:
    (workspace.root / "src" / "pkg" / "huge.py").write_text("X = 1\n" * 2000)
    with store_for(workspace.root) as db:
        stats = reconcile(workspace, store=db, max_file_bytes=4096)
        assert stats.skipped == 1
        assert stats.parsed == 2
        assert "src/pkg/huge.py" not in db.known_files()


def test_the_walk_stops_at_max_files(workspace: Workspace) -> None:
    for index in range(10):
        (workspace.root / "src" / "pkg" / f"m{index}.py").write_text("A = 1\n")
    with store_for(workspace.root) as db:
        stats = reconcile(workspace, store=db, max_files=4)
        assert stats.scanned == 4


def test_gitignored_and_noisy_directories_are_skipped(workspace: Workspace) -> None:
    (workspace.root / ".gitignore").write_text("generated/\n")
    (workspace.root / "generated").mkdir()
    (workspace.root / "generated" / "gen.py").write_text("GEN = 1\n")
    (workspace.root / "node_modules" / "dep").mkdir(parents=True)
    (workspace.root / "node_modules" / "dep" / "index.py").write_text("DEP = 1\n")

    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        indexed = set(db.known_files())
        assert indexed == {"src/pkg/__init__.py", "src/pkg/engine.py"}
        assert "node_modules" in SKIP_DIRS


def test_denylisted_secrets_are_never_opened(tmp_path: Path) -> None:
    """The walker must not be a second path around the workspace denylist."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("A = 1\n")
    (root / "settings.py").write_text("B = 2\n")
    secret = root / "secret.pem"
    secret.write_text("PRIVATE = 1\n")

    workspace = Workspace(root=root)
    assert not workspace.is_allowed(secret)
    with store_for(root) as db:
        reconcile(workspace, store=db)
        assert "secret.pem" not in db.known_files()


# --------------------------------------------------------------- import graph


def test_absolute_imports_resolve_through_a_src_layout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("")
    (root / "src" / "pkg" / "base.py").write_text("BASE = 1\n")
    (root / "src" / "pkg" / "uses.py").write_text("from pkg.base import BASE\n")

    workspace = Workspace(root=root)
    with store_for(root) as db:
        reconcile(workspace, store=db)
        assert db.importers_of("src/pkg/base.py") == ["src/pkg/uses.py"]


def test_relative_imports_resolve_against_the_importing_package(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    for part in ("pkg", "pkg/sub"):
        (root / part / "__init__.py").write_text("")
    (root / "pkg" / "shared.py").write_text("SHARED = 1\n")
    (root / "pkg" / "sub" / "leaf.py").write_text("from ..shared import SHARED\n")

    workspace = Workspace(root=root)
    with store_for(root) as db:
        reconcile(workspace, store=db)
        assert db.importers_of("pkg/shared.py") == ["pkg/sub/leaf.py"]


def test_imports_that_point_outside_the_repo_stay_unresolved(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        edges = dict(db.imports_of("src/pkg/engine.py"))
        assert edges["os"] == ""
        assert edges["pathlib"] == ""
        assert edges["altus.core.errors"] == ""
        # `from . import sibling` does point at a file in this repo, and the
        # package's own __init__ is that file.
        assert db.imports_of("src/pkg/engine.py", internal_only=True) == [
            (".", "src/pkg/__init__.py")
        ]


def test_fan_in_ranks_the_most_imported_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "core.py").write_text("CORE = 1\n")
    (root / "quiet.py").write_text("QUIET = 1\n")
    for index in range(3):
        (root / f"user{index}.py").write_text("import core\n")
    (root / "one.py").write_text("import quiet\n")

    workspace = Workspace(root=root)
    with store_for(root) as db:
        reconcile(workspace, store=db)
        assert db.fan_in(2) == [("core.py", 3), ("quiet.py", 1)]


# -------------------------------------------------------------------- storage


def test_the_map_lives_outside_the_repository(tmp_path: Path, workspace: Workspace) -> None:
    path = map_path(workspace.root)
    assert tmp_path / "data" in path.parents
    assert workspace.root not in path.parents


def test_two_checkouts_of_one_name_get_two_maps(tmp_path: Path) -> None:
    first = tmp_path / "a" / "repo"
    second = tmp_path / "b" / "repo"
    for path in (first, second):
        path.mkdir(parents=True)
    assert map_path(first) != map_path(second)
    assert map_path(first).name.startswith("repo-")


def test_a_schema_change_rebuilds_rather_than_migrating(workspace: Workspace) -> None:
    path = map_path(workspace.root)
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        assert db.file_count() == 2
        db.set_meta("schema_version", "0")

    with MapStore(path, workspace.root) as rebuilt:
        assert rebuilt.file_count() == 0
        reconcile(workspace, store=rebuilt)
        assert rebuilt.file_count() == 2


def test_find_prefers_exported_definitions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "m.py").write_text("def _config() -> None: ...\n\nclass Config: ...\n")
    workspace = Workspace(root=root)
    with store_for(root) as db:
        reconcile(workspace, store=db)
        hits = db.find("config")
        assert [symbol.name for _, symbol in hits] == ["Config", "_config"]


def test_find_can_filter_by_kind(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        assert [s.name for _, s in db.find("", kind="class")] == ["Engine"]


def test_directories_tally_files_and_symbols(workspace: Workspace) -> None:
    with store_for(workspace.root) as db:
        reconcile(workspace, store=db)
        assert db.directories() == [("src/pkg", 2, pytest.approx(9, abs=3))]


# ------------------------------------------------------ the absent extractor


def test_a_language_with_no_extractor_is_reported_not_silently_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode this guards against: a Go repository indexed without
    the extra looks exactly like a Go repository containing nothing."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.go").write_text("package main\n\nfunc Hello() {}\n")
    (root / "ok.py").write_text("A = 1\n")

    monkeypatch.setattr(
        "altus.repomap.index.extractor_for",
        lambda language: PythonExtractor() if language == "python" else None,
    )
    with store_for(root) as db:
        stats = reconcile(Workspace(root=root), store=db)

        assert stats.unsupported == {"go": 1}
        assert stats.parsed == 1
        assert "main.go" not in db.known_files()

    message = describe_absence(stats.unsupported)
    assert "1 go" in message
    assert "altus[repomap]" in message


def test_nothing_missing_means_no_message() -> None:
    assert describe_absence({}) == ""
