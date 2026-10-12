"""Churn from git history, and the rendered map a person actually reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from altus.repomap import churn, digest
from altus.repomap.index import reconcile
from altus.repomap.store import store_for
from altus.workspace import Workspace


def _run(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real checkout. Churn reads git, so a fake would test nothing."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    _run(root.parent, "init", "-q", "-b", "main", str(root))
    _run(root, "config", "user.email", "one@example.com")
    _run(root, "config", "user.name", "One")

    (root / "src" / "busy.py").write_text("A = 1\n")
    (root / "src" / "quiet.py").write_text("B = 2\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", "first")

    for index in range(3):
        (root / "src" / "busy.py").write_text(f"A = {index}\n")
        _run(root, "add", "-A")
        _run(root, "commit", "-q", "-m", f"touch {index}")
    return root


async def test_churn_counts_commits_per_file(repo: Path) -> None:
    rows = dict((path, count) for path, count, _authors, _seen in await churn.collect(repo))
    assert rows["src/busy.py"] == 4
    assert rows["src/quiet.py"] == 1


async def test_churn_counts_distinct_authors(repo: Path) -> None:
    _run(repo, "config", "user.name", "Two")
    _run(repo, "config", "user.email", "two@example.com")
    (repo / "src" / "busy.py").write_text("A = 9\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "second author")

    rows = {path: authors for path, _count, authors, _seen in await churn.collect(repo)}
    assert rows["src/busy.py"] == 2
    assert rows["src/quiet.py"] == 1


async def test_churn_records_the_most_recent_date(repo: Path) -> None:
    rows = {path: seen for path, _count, _authors, seen in await churn.collect(repo)}
    assert rows["src/busy.py"]
    assert rows["src/busy.py"] == rows["src/quiet.py"]  # same day in a test run


async def test_merge_commits_are_not_counted(repo: Path) -> None:
    """A merge touches everything both sides touched, so counting merges makes
    whatever sat near a long branch look like the busiest code in the tree."""
    _run(repo, "checkout", "-q", "-b", "side")
    (repo / "src" / "quiet.py").write_text("B = 3\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "on the side")
    _run(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("C = 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "on main")
    _run(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")

    rows = {path: count for path, count, _a, _s in await churn.collect(repo)}
    assert rows["src/quiet.py"] == 2  # the first commit and the side one, not the merge


async def test_churn_is_skipped_when_head_has_not_moved(repo: Path) -> None:
    with store_for(repo) as db:
        first = await churn.update(db, repo)
        assert first > 0
        assert await churn.update(db, repo) == 0

        (repo / "src" / "busy.py").write_text("A = 99\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "moved")
        assert await churn.update(db, repo) > 0


async def test_a_directory_that_is_not_a_checkout_has_no_churn(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await churn.head(plain) == ""
    with store_for(plain) as db:
        assert await churn.update(db, plain) == 0


async def test_hotspots_only_names_files_that_still_exist(repo: Path) -> None:
    """git remembers deleted files; a list of hotspots pointing at paths that
    are gone is a list of dead ends."""
    (repo / "src" / "quiet.py").unlink()
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "delete quiet")

    workspace = Workspace(root=repo)
    with store_for(repo) as db:
        reconcile(workspace, store=db)
        await churn.update(db, repo)
        paths = [path for path, _c, _a, _s in db.hotspots()]
        assert "src/busy.py" in paths
        assert "src/quiet.py" not in paths


# ------------------------------------------------------------------ rendering


@pytest.fixture
def mapped(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src" / "core").mkdir(parents=True)
    (root / "src" / "core" / "__init__.py").write_text('"""The core of it."""\n')
    (root / "src" / "core" / "engine.py").write_text(
        '"""Runs the thing."""\n\n\nclass Engine:\n    """The engine."""\n\n'
        "    def start(self) -> None: ...\n"
    )
    (root / "src" / "app.py").write_text('"""Entry point."""\n\nfrom core.engine import Engine\n')
    return root


def test_overview_names_the_repository_and_its_shape(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        text = digest.overview(db)

        assert "proj: 3 files" in text
        assert "python 3" in text
        assert "Packages" in text
        assert "The core of it." in text


def test_a_packages_own_init_docstring_wins_over_a_modules(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        found = {package.path: package.doc for package in digest.packages(db)}
        assert found["src/core"] == "The core of it."


def test_a_package_with_no_docstring_falls_back_to_what_it_exports(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    (root / "thing.py").write_text("class Widget: ...\n\n\ndef helper() -> None: ...\n")
    with store_for(root) as db:
        reconcile(Workspace(root=root), store=db)
        text = digest.overview(db)
        assert "Widget" in text


def test_overview_says_so_when_nothing_is_indexed(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with store_for(root) as db:
        assert "empty" in digest.overview(db).lower()


def test_overview_omits_churn_when_there_is_none(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        assert "Where the work is" not in digest.overview(db)


def test_file_card_lists_definitions_with_lines_to_jump_to(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        card = digest.file_card(db, "src/core/engine.py")

        assert "Runs the thing." in card
        assert "class Engine" in card
        assert "    4  " in card
        assert "imported by 1" in card


def test_file_card_says_so_for_an_unindexed_path(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        card = digest.file_card(db, "src/nope.py")
        assert "not in the map" in card


def test_symbol_card_finds_a_definition_by_name(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        card = digest.symbol_card(db, "Engine")

        assert "src/core/engine.py:4" in card
        assert "class Engine" in card


def test_symbol_card_is_explicit_about_finding_nothing(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        assert "no definition matching" in digest.symbol_card(db, "Nonexistent")


def test_package_card_lists_the_files_under_a_directory(mapped: Path) -> None:
    with store_for(mapped) as db:
        reconcile(Workspace(root=mapped), store=db)
        card = digest.package_card(db, "src/core")

        assert "engine.py" in card
        assert "app.py" not in card


def test_age_is_never_rounded_up_into_sounding_fresher() -> None:
    now = 1_000_000.0
    assert digest._ago(0) == "never"
    assert digest._ago(now - 5, now=now) == "just now"
    assert digest._ago(now - 119, now=now) == "1 minute ago"
    assert digest._ago(now - 7199, now=now) == "1 hour ago"
    assert digest._ago(now - 86400 * 2.9, now=now) == "2 days ago"
