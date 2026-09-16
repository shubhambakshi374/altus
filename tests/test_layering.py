"""The headless guard.

Every package listed below must stay importable without Textual, because the
workflow engine drives them with no terminal attached. That was a promise for
five phases; ``altus.workflow`` is the first thing collecting on it, which is
exactly why it is on the list. If this test fails, do not delete it --- move
the offending code into ``altus.tui`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import altus

HEADLESS_PACKAGES = (
    "core",
    "providers",
    "config",
    "storage",
    "tools",
    "cloud",
    "mcp",
    "render",
    "workflow",
)
SRC = Path(altus.__file__).parent


def _modules(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", HEADLESS_PACKAGES)
def test_headless_packages_do_not_import_textual(package: str) -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _modules(package)
        if "textual" in _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"textual imported by headless module(s): {offenders}"


@pytest.mark.parametrize("package", HEADLESS_PACKAGES)
def test_headless_packages_do_not_import_tui(package: str) -> None:
    for path in _modules(package):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("altus.tui"):
                pytest.fail(f"{path.relative_to(SRC)} imports {node.module}")


HEADLESS_MODULES = ("runner.py", "workspace.py", "agent.py")


@pytest.mark.parametrize("module", HEADLESS_MODULES)
def test_headless_modules_do_not_import_textual(module: str) -> None:
    path = SRC / module
    if not path.exists():
        pytest.skip(f"{module} not built yet")
    assert "textual" not in _imported_roots(ast.parse(path.read_text(encoding="utf-8")))


# --------------------------------------------------- test isolation guard


#: Replacing this wholesale discards everything conftest put in the
#: environment --- the provider key, the AWS credential blocks --- so the app
#: finds nothing configured and opens the first-run wizard over the chat
#: screen. A developer's own ~/.aws then makes a provider look configured and
#: the wizard does not open, so the suite passes locally and fails on a runner.
#: That divergence has now cost two debugging sessions; this is cheaper.
def _replaces_environ(tree: ast.AST) -> list[int]:
    """Calls to ``setattr(..., "os.environ", ...)``, by line.

    Matched in the AST rather than the text so the prose explaining the rule
    does not trip it --- the first version of this guard failed on its own
    docstring.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name != "setattr":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == "os.environ":
            found.append(node.lineno)
    return found


def _test_modules() -> list[Path]:
    return sorted(Path(__file__).parent.glob("test_*.py"))


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_no_test_replaces_the_environment_wholesale(path: Path) -> None:
    offenders = _replaces_environ(ast.parse(path.read_text(encoding="utf-8")))
    assert not offenders, (
        f"{path.name}:{offenders} replaces os.environ. Use monkeypatch.setenv "
        "and delenv for the variables you care about --- replacing the mapping "
        "throws away the isolation conftest set up, and the result is a test "
        "that passes on your machine and fails in CI."
    )
