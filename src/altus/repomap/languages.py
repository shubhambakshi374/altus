"""Which languages the map understands, and who extracts each one.

Python is handled here, by the standard library's ``ast``. That is a
deliberate asymmetry: ``ast`` ships with the interpreter, costs nothing to
install, and is not an approximation of Python --- it is the same parser the
interpreter uses, so a definition it reports is a definition that exists.

Everything else goes through tree-sitter, in ``treesitter``, behind the
``repomap`` extra. The registry below reports what it could *not* do as well as
what it could: a Go repository indexed without the extra should say "840 Go
files, no extractor installed", because a map that silently contains nothing is
indistinguishable from a repository that contains nothing.
"""

from __future__ import annotations

import ast
import logging
from typing import Protocol, runtime_checkable

from altus.repomap.models import Extracted, Import, Symbol

log = logging.getLogger(__name__)

#: Extension -> language. The language name is what the store records and what
#: a caller filters on, so it is the tree-sitter spelling where one exists.
EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}

#: Languages that need the extra. Named rather than derived, so the message
#: about a missing extractor can be written before the import is attempted.
NEEDS_EXTRA = ("go", "typescript", "tsx", "javascript", "rust", "java", "ruby")

EXTRA_HINT = "install the extra to index it: uv pip install 'altus[repomap]'"


def language_of(path: str) -> str:
    """The language of a path by extension, or "" for one we do not parse."""
    dot = path.rfind(".")
    return EXTENSIONS.get(path[dot:].lower(), "") if dot > 0 else ""


@runtime_checkable
class Extractor(Protocol):
    """Text in, definitions out. No filesystem access, no network, no state.

    Deliberately pure: the walker reads bytes once and decides what is worth
    parsing, and an extractor that could read on its own would be a second
    path around ``Workspace.resolve``.
    """

    def extract(self, text: str, path: str) -> Extracted: ...


def extractor_for(language: str) -> Extractor | None:
    """The extractor for a language, or None when there is nothing to do it.

    The tree-sitter import is attempted lazily and its failure is not an error:
    this is the function that turns "the extra is not installed" into a
    reportable absence rather than a traceback at the first Go file.
    """
    if language == "python":
        return PythonExtractor()
    try:
        from altus.repomap.treesitter import extractor as ts_extractor
    except ImportError as exc:
        log.debug("tree-sitter extractors unavailable: %s", exc)
        return None
    return ts_extractor(language)


def available_languages() -> list[str]:
    """Languages this installation can actually index, in a stable order."""
    seen = {name for name in EXTENSIONS.values()}
    return sorted(name for name in seen if extractor_for(name) is not None)


def missing_languages() -> list[str]:
    """Languages we recognise by extension but cannot parse here."""
    seen = {name for name in EXTENSIONS.values()}
    return sorted(name for name in seen if extractor_for(name) is None)


# --------------------------------------------------------------------- python


#: Module-level names in these shapes are worth recording; a bare assignment
#: to a lowercase name is a variable, and a map full of them is a map nobody
#: reads.
def _is_constant_name(name: str) -> bool:
    return name.isupper() or (name[:1].isupper() and "_" in name)


class PythonExtractor:
    """Definitions and imports from Python, via ``ast``.

    Nested functions are not recorded. A closure defined inside a function is
    an implementation detail of that function, and the thing a reader is
    looking for is the function; recording both puts two answers in the index
    for one question. Methods *are* recorded, with their class as ``parent``,
    because ``Workspace.resolve`` is what somebody greps for and it is a
    method.
    """

    language = "python"

    def extract(self, text: str, path: str) -> Extracted:
        tree = ast.parse(text)
        doc = _first_line(ast.get_docstring(tree) or "")
        exported = _dunder_all(tree)
        symbols: list[Symbol] = []
        imports: list[Import] = []

        for node in tree.body:
            self._top_level(node, symbols, imports, exported)
        return Extracted(doc=doc, symbols=tuple(symbols), imports=tuple(imports))

    def _top_level(
        self,
        node: ast.stmt,
        symbols: list[Symbol],
        imports: list[Import],
        exported: frozenset[str] | None,
    ) -> None:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    exported=_is_exported(node.name, exported),
                    signature=_class_signature(node),
                    doc=_first_line(ast.get_docstring(node) or ""),
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        Symbol(
                            name=child.name,
                            kind="method",
                            line=child.lineno,
                            end_line=child.end_lineno or child.lineno,
                            parent=node.name,
                            exported=_is_exported(child.name, None),
                            signature=_def_signature(child),
                            doc=_first_line(ast.get_docstring(child) or ""),
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    exported=_is_exported(node.name, exported),
                    signature=_def_signature(node),
                    doc=_first_line(ast.get_docstring(node) or ""),
                )
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assigned_names(node):
                if _is_constant_name(name):
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="constant",
                            line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            exported=_is_exported(name, exported),
                        )
                    )
        elif isinstance(node, ast.Import):
            imports += [Import(target=alias.name, line=node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # A relative import is written back with its dots intact: the
            # importer's own package is what resolves it, and only the walker
            # knows that. `from . import x` has no module at all, which is why
            # the dots alone have to be a usable target.
            target = "." * node.level + (node.module or "")
            imports.append(Import(target=target, line=node.lineno))
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` holds imports that are real edges of the
            # graph even though they never execute, and a module whose whole
            # body sits under `if sys.version_info` is not unheard of.
            for child in node.body:
                self._top_level(child, symbols, imports, exported)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _dunder_all(tree: ast.Module) -> frozenset[str] | None:
    """``__all__`` as a set, or None when the module does not declare one.

    None and the empty set mean different things --- "no opinion, fall back to
    the underscore convention" versus "nothing is public" --- so this cannot
    return a bare set.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return frozenset(
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return None


def _is_exported(name: str, declared: frozenset[str] | None) -> bool:
    if declared is not None:
        return name in declared
    return not name.startswith("_")


def _def_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    try:
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    except ValueError, AttributeError, RecursionError:
        return prefix + node.name + "(...)"
    return f"{prefix}{node.name}({args}){returns}"


def _class_signature(node: ast.ClassDef) -> str:
    try:
        bases = [ast.unparse(base) for base in node.bases]
    except ValueError, AttributeError, RecursionError:
        bases = []
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def _first_line(text: str) -> str:
    """The first sentence-ish line of a docstring, collapsed.

    Whole docstrings do not belong in an index whose point is to be cheaper
    than reading the file. The summary line is the part somebody wrote for
    exactly this purpose.
    """
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def describe_absence(unsupported: dict[str, int]) -> str:
    """One line naming what could not be indexed, or "" when nothing was missed."""
    if not unsupported:
        return ""
    parts = [f"{count} {lang}" for lang, count in sorted(unsupported.items())]
    return f"not indexed: {', '.join(parts)} ({EXTRA_HINT})"
