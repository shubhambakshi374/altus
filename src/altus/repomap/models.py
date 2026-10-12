"""What the index stores. Dataclasses, not pydantic models.

Nothing here crosses a trust boundary --- these are built by our own
extractors from files we just read and handed to our own SQLite schema --- so
validation would be ceremony, and a repo map that allocated a pydantic model
per symbol would be paying for it 50,000 times on a medium codebase.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The vocabulary every extractor normalises onto. Languages disagree about
#: names for the same thing --- Go's ``type`` is Java's ``class`` is Rust's
#: ``struct`` --- and a caller asking "where are the types" should not have to
#: know which language it landed in to phrase the question.
KINDS = (
    "class",
    "function",
    "method",
    "type",
    "interface",
    "constant",
    "module",
)


@dataclass(frozen=True, slots=True)
class Symbol:
    """One definition, and where to go and read it."""

    name: str
    kind: str
    line: int
    end_line: int = 0
    parent: str = ""
    """The enclosing definition, for a method on a class. Empty at top level."""
    exported: bool = True
    """Public by the language's own convention --- leading underscore in
    Python, leading capital in Go. Languages with no such convention say True,
    because guessing would be worse than declining to."""
    signature: str = ""
    doc: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass(frozen=True, slots=True)
class Import:
    """One edge of the import graph, as written and where it points.

    ``resolved`` is filled only when the target is a file in this repository.
    An unresolved import is not a failure --- ``import os`` has nowhere in the
    tree to point --- and conflating the two would make every stdlib import
    look like a broken edge.
    """

    target: str
    line: int
    resolved: str = ""


@dataclass(frozen=True, slots=True)
class Extracted:
    """Everything one extractor found in one file."""

    doc: str = ""
    """The module's own one-line description, where the language has such a
    thing. In a codebase that writes them, this is the best summary of a file
    that exists, and it was already written by someone who knew."""
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[Import, ...] = ()


@dataclass(frozen=True, slots=True)
class FileFacts:
    """One row of the file table: identity, size, and how we knew it changed."""

    path: str
    """Posix, relative to the repository root. The key everywhere."""
    language: str
    size: int
    mtime_ns: int
    digest: str
    """sha256 of the bytes. ``mtime`` and ``size`` are the cheap gate; this is
    the answer. A file touched by a checkout that restores identical content
    has a new mtime and must not be re-parsed."""
    lines: int
    doc: str = ""
    symbols: int = 0


@dataclass(slots=True)
class IndexStats:
    """What one reconcile pass did, in the terms a person would ask about."""

    scanned: int = 0
    parsed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    """Recognised language, but too large to parse."""
    failed: list[str] = field(default_factory=list)
    """Paths whose parse raised. A syntax error in one file must not fail the
    index --- half a map is worth more than none, and a repo mid-refactor
    always has one."""
    unsupported: dict[str, int] = field(default_factory=dict)
    """Language -> file count, for languages we recognised but have no
    extractor for. This is what makes a missing extra visible."""
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.scanned
