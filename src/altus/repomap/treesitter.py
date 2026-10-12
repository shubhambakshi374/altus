"""Non-Python extractors, through tree-sitter. Behind the ``repomap`` extra.

Three things about how this is wired are deliberate.

**Per-language grammar wheels, not a language pack.** The obvious dependency
is ``tree-sitter-language-pack``, which offers 160 languages behind one
import. Its current release ships *no* grammars: it downloads them from the
network on first use and loads the result as a shared library. A map whose
first run fetches and ``dlopen``s a binary is not a local index, and "it is
only a grammar" is exactly the argument that makes supply-chain compromises
work. The individual ``tree-sitter-<language>`` wheels bundle a compiled
grammar, are pinned in ``pyproject.toml``, and need no network ever.

**The queries are the grammars' own.** Each wheel ships ``queries/tags.scm``
--- the same tag query GitHub's code navigation uses, written by the people
who wrote the grammar. Hand-rolling node-type lists per language would be
re-deriving that, worse, in a file nobody would maintain.

**A language with no tags query is not supported.** Bash, HCL and C# ship
grammars without one. Rather than invent queries for them, they are absent
from ``EXTENSIONS`` --- a language the map claims to understand and silently
finds nothing in is worse than one it says it cannot read.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import tree_sitter

from altus.repomap.models import Extracted, Import, Symbol

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Spec:
    """Where one language's grammar and tag queries come from.

    ``factory`` is named because TypeScript ships two grammars in one wheel
    and the dialect is the whole point of the distinction.

    ``queries`` is a list because TypeScript's own ``tags.scm`` is written to
    be *concatenated* with JavaScript's, not used alone: the TypeScript
    grammar extends the JavaScript one, so its query only adds the patterns
    TypeScript introduces --- ``function_signature``,
    ``abstract_class_declaration``. Run on its own it finds no ordinary
    function or class at all, which is a silent and very convincing kind of
    empty map.
    """

    module: str
    factory: str
    queries: tuple[str, ...] = ()
    """Modules whose ``tags.scm`` to concatenate, in order. Empty means the
    grammar's own query is complete."""


GRAMMARS: dict[str, Spec] = {
    "go": Spec("tree_sitter_go", "language"),
    "typescript": Spec(
        "tree_sitter_typescript",
        "language_typescript",
        ("tree_sitter_javascript", "tree_sitter_typescript"),
    ),
    "tsx": Spec(
        "tree_sitter_typescript",
        "language_tsx",
        ("tree_sitter_javascript", "tree_sitter_typescript"),
    ),
    "javascript": Spec("tree_sitter_javascript", "language"),
    "rust": Spec("tree_sitter_rust", "language"),
    "java": Spec("tree_sitter_java", "language"),
    "ruby": Spec("tree_sitter_ruby", "language"),
}

#: The tags query's capture names, mapped onto our own vocabulary. A capture
#: we do not recognise is dropped rather than guessed at: ``definition.x``
#: for an unknown x is a new language convention, not a new kind of thing.
KINDS: dict[str, str] = {
    "definition.class": "class",
    "definition.function": "function",
    "definition.method": "method",
    "definition.type": "type",
    "definition.interface": "interface",
    "definition.struct": "type",
    "definition.enum": "type",
    "definition.module": "module",
    "definition.constant": "constant",
    "definition.constructor": "method",
    "definition.trait": "interface",
    "definition.field": "constant",
    "definition.macro": "function",
}

#: Which kind wins when two patterns match the same definition. Rust's
#: ``tags.scm`` matches a function inside an ``impl`` block as both a method
#: and a function, and reporting ``Config::new`` twice --- once under each ---
#: puts two rows in the index for one definition.
KIND_RANK: dict[str, int] = {
    "method": 0,
    "class": 1,
    "type": 1,
    "interface": 1,
    "module": 1,
    "constant": 1,
    "function": 2,
}


#: How each language says "exported". Go is the only one of these with a rule
#: the compiler enforces; the rest have conventions a reader applies and a
#: linter does not, so claiming to know would be worse than declining to.
def _exported(language: str, name: str) -> bool:
    if language == "go":
        return name[:1].isupper()
    if language == "ruby":
        return not name.startswith("_")
    return True


@dataclass(frozen=True, slots=True)
class Grammar:
    """A loaded grammar and its tags query, ready to run."""

    language: str
    ts_language: Any
    query: Any


@cache
def grammar(language: str) -> Grammar | None:
    """Load one grammar, or None when its wheel is not installed.

    Cached because compiling a tags query is the expensive part and a repo
    map parses thousands of files against the same handful of grammars.
    """
    spec = GRAMMARS.get(language)
    if spec is None:
        return None
    try:
        module = importlib.import_module(spec.module)
        sources = [importlib.import_module(name) for name in (spec.queries or (spec.module,))]
    except ImportError:
        log.debug("no grammar installed for %s (%s)", language, spec.module)
        return None

    try:
        ts_language = tree_sitter.Language(getattr(module, spec.factory)())
        query = tree_sitter.Query(ts_language, "\n".join(_tags_query(m) for m in sources))
    except (AttributeError, OSError, ValueError, tree_sitter.QueryError) as exc:
        # A grammar whose ABI does not match the installed tree-sitter, or a
        # wheel that moved its queries. Reported as an absence, because the
        # alternative is one unparseable file ending the whole index.
        log.warning("grammar for %s is unusable: %s", language, exc)
        return None
    return Grammar(language=language, ts_language=ts_language, query=query)


def _tags_query(module: Any) -> str:
    """The wheel's own tags query.

    Most expose it as ``TAGS_QUERY``; the rest only ship the file. Both are
    read from the installed package, never from the network.
    """
    text = getattr(module, "TAGS_QUERY", "")
    if isinstance(text, str) and text.strip():
        return text
    path = Path(module.__file__).parent / "queries" / "tags.scm"
    return path.read_text(encoding="utf-8")


def extractor(language: str) -> TreeSitterExtractor | None:
    """The extractor for a language, or None when its grammar is absent."""
    loaded = grammar(language)
    return TreeSitterExtractor(loaded) if loaded is not None else None


class TreeSitterExtractor:
    """Definitions and imports from one tree-sitter grammar.

    Definitions and their names are read per *pattern match*, not from the
    flat capture list. The flat list is tempting and wrong: Go writes
    ``func (s *Server) Start()``, where the first ``@name`` inside the method
    node is the receiver type, so pairing by containment reports every Go
    method as being called after its struct. ``matches()`` keeps each
    pattern's captures together, which is what the query actually declared.
    """

    def __init__(self, loaded: Grammar) -> None:
        self._grammar = loaded
        self.language = loaded.language

    def extract(self, text: str, path: str) -> Extracted:
        raw = text.encode("utf-8")
        parser = tree_sitter.Parser(self._grammar.ts_language)
        tree = parser.parse(raw)
        cursor = tree_sitter.QueryCursor(self._grammar.query)

        symbols: list[Symbol] = []
        for _pattern, captures in cursor.matches(tree.root_node):
            kind = next((KINDS[key] for key in captures if key in KINDS), "")
            nodes = captures.get("name", ())
            if not kind or not nodes:
                continue
            definition = next(captures[key][0] for key in captures if key in KINDS)
            name = _text(nodes[0])
            if not name:
                continue
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=definition.start_point[0] + 1,
                    end_line=definition.end_point[0] + 1,
                    exported=_exported(self.language, name),
                    signature=_signature(definition),
                    doc=_doc(captures),
                )
            )
        return Extracted(
            doc="",
            symbols=_deduplicated(symbols),
            imports=tuple(_imports(tree.root_node, self.language)),
        )


def _deduplicated(symbols: list[Symbol]) -> tuple[Symbol, ...]:
    """One row per definition, in source order, most specific kind winning."""
    best: dict[tuple[str, int], Symbol] = {}
    for symbol in symbols:
        key = (symbol.name, symbol.line)
        held = best.get(key)
        if held is None or KIND_RANK.get(symbol.kind, 9) < KIND_RANK.get(held.kind, 9):
            best[key] = symbol
    return tuple(sorted(best.values(), key=lambda symbol: (symbol.line, symbol.name)))


def _text(node: Any) -> str:
    return str(node.text.decode("utf-8", errors="replace")) if node.text else ""


def _signature(node: Any) -> str:
    """The definition's first line, trimmed.

    Not a reconstructed signature: these are seven languages with seven
    grammars, and the first line of a definition is what a reader would see
    anyway. A brace at the end is noise, so it goes.
    """
    first = _text(node).split("\n", 1)[0].strip()
    body = first.find("{")
    if body > 0:
        first = first[:body]
    return first.strip().rstrip("=(,").strip()[:200]


def _doc(captures: dict[str, Any]) -> str:
    """The comment the tags query attached to this definition, if any.

    The queries capture it as ``@doc`` within the same pattern, with a
    ``#set-adjacent!`` predicate --- so it arrives already associated, and
    nothing here has to guess which comment belongs to which definition.
    """
    nodes = captures.get("doc", ())
    if not nodes:
        return ""
    text = _text(nodes[0]).lstrip("/#*- \t").strip()
    return text.split("\n", 1)[0].strip("*/ ")[:200]


#: Node types that hold an import, by language family. Imports are *not* in
#: the tags queries --- those describe definitions and references --- so this
#: is the one place where node types are named by hand, and it is kept to the
#: single node type per language that carries a module string.
IMPORT_NODES = frozenset(
    {
        "import_declaration",  # go, java
        "import_spec",  # go
        "import_statement",  # javascript, typescript, ruby-ish
        "import_from_statement",
        "use_declaration",  # rust
        "call",  # ruby: require "x"
        "call_expression",  # javascript: require("x")
    }
)


def _imports(root: Any, language: str) -> list[Import]:
    """Module strings this file imports, as written.

    Nothing is resolved here --- resolution needs the whole file set, and the
    walker does it afterwards. Go and Java module paths will not resolve to a
    repository path at all, and that is recorded honestly as an edge pointing
    outwards rather than dropped.
    """
    first: dict[str, int] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in IMPORT_NODES:
            for target in _module_strings(node, language):
                # Deduplicated by target, not by (target, line). Go nests an
                # `import_spec` inside an `import_declaration`, so the same
                # module matches twice at two different lines, and a graph
                # with one edge per spelling of one import is a graph that
                # double-counts fan-in.
                line = node.start_point[0] + 1
                if target not in first or line < first[target]:
                    first[target] = line
        stack.extend(node.children)
    return sorted(
        (Import(target=target, line=line) for target, line in first.items()),
        key=lambda imp: (imp.line, imp.target),
    )


def _module_strings(node: Any, language: str) -> list[str]:
    """The quoted or pathed module names inside one import node."""
    if node.type in ("call", "call_expression"):
        # `require "sinatra"` and `require("./thing")` are calls, so the node
        # type alone does not mean import; the callee name does.
        callee = node.child_by_field_name("function") or node.child_by_field_name("method")
        if callee is None or _text(callee) not in ("require", "require_relative"):
            return []

    out: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("interpreted_string_literal", "string_literal", "string"):
            text = _text(current).strip("\"'`")
            if text:
                out.append(text)
            continue
        if (
            language in ("rust", "java")
            and current.type == "scoped_identifier"
            and current is not node
        ):
            # Left as the language writes it. Rewriting Rust's `::` to `.`
            # would make the target look resolvable by the Python rules, and
            # it is not: a Rust path is relative to a crate and a Java one to
            # the classpath, neither of which is this directory.
            out.append(_text(current))
            continue
        stack.extend(current.children)
    return out
