"""The map as a person reads it: packages, what depends on what, where the work is.

The index answers precise questions. This answers the vague one --- *what is
this repository* --- which is the question somebody arriving actually has, and
the one that costs the most tool calls to answer by groping around.

Everything here is derived, not written. No model is asked to describe a
package: the description is the module docstring its author already wrote,
and where there is none, the shape of the thing speaks instead (how many
files, what they export, who depends on them). That keeps the map free, exact,
and identical on every machine --- and means a repository that documents
itself gets a better map than one that does not, which is the right incentive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from altus.repomap.store import MapStore

#: A package summary is for orientation, so it is held to a line. Anything
#: longer and the overview stops being cheaper than reading the directory.
SUMMARY = 96


@dataclass(slots=True)
class Package:
    """One directory of code, and the one line that describes it."""

    path: str
    files: int = 0
    lines: int = 0
    symbols: int = 0
    doc: str = ""
    exports: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


def packages(db: MapStore, *, limit: int = 40) -> list[Package]:
    """Every directory holding code, largest first, with a one-line summary.

    Directories rather than language packages: a repository has a shape on
    disk, and that shape is what somebody reading it navigates. Deriving
    Python packages here would describe one language's view of a tree that
    may hold four.
    """
    rows = db.all_files()
    tally: dict[str, Package] = {}
    for path, language, lines, doc, symbols in rows:
        directory = path.rpartition("/")[0] or "."
        entry = tally.setdefault(directory, Package(path=directory))
        entry.files += 1
        entry.lines += lines
        entry.symbols += symbols
        if language not in entry.languages:
            entry.languages.append(language)
        # A package's own __init__ is the file whose docstring describes the
        # package rather than one module in it, so it wins outright.
        base = path.rpartition("/")[2]
        if doc and (not entry.doc or base.startswith("__init__")):
            entry.doc = doc[:SUMMARY]

    for package in tally.values():
        package.languages.sort()
        package.exports = [
            symbol.name
            for symbol in db.top_exports(package.path, limit=6)
            if symbol.kind in ("class", "type", "interface", "function", "method")
        ]
    ordered = sorted(tally.values(), key=lambda item: (-item.lines, item.path))
    return ordered[:limit]


def overview(db: MapStore, *, name: str = "", packages_shown: int = 24) -> str:
    """The whole map, as the text a model or a person is handed.

    Ordered by what somebody needs first: the size of the thing, then its
    shape, then what the rest of it leans on, then where the work is
    happening. A reader who stops after three lines has still learned the
    useful part.
    """
    files = db.file_count()
    if not files:
        return "The repo map is empty. Nothing has been indexed yet."

    languages = ", ".join(f"{language} {count}" for language, count in db.languages())
    lines = [
        f"{name or db.root.name}: {files} files, {db.symbol_count()} symbols ({languages})",
        f"indexed {_ago(db.built_at)}",
        "",
        "Packages",
    ]
    for package in packages(db, limit=packages_shown):
        summary = package.doc or (", ".join(package.exports[:4]) if package.exports else "")
        size = f"{package.files} files, {_thousands(package.lines)} lines"
        lines.append(f"  {package.path:<34} {size:>20}  {summary}")

    leaned_on = db.fan_in(10)
    if leaned_on:
        docs = db.docs_for([path for path, _ in leaned_on])
        lines += ["", "Most depended on"]
        for path, count in leaned_on:
            lines.append(f"  {count:>4}  {path:<44} {docs.get(path, '')[:SUMMARY]}")

    hot = db.hotspots(10)
    if hot:
        window = db.get_meta("churn_commits", "")
        suffix = f" (last {window} commits)" if window else ""
        lines += ["", f"Where the work is{suffix}"]
        for path, commits, authors, seen in hot:
            who = f"{authors} author" + ("s" if authors != 1 else "")
            lines.append(f"  {commits:>4}  {path:<44} {who}, last {seen or 'unknown'}")

    return "\n".join(lines)


def package_card(db: MapStore, directory: str) -> str:
    """One directory in full: its files, and what each one is."""
    files = db.files_under(directory)
    if not files:
        return f"nothing indexed under {directory!r}"

    lines = [f"{directory} — {len(files)} files"]
    for path, _language, _lines, doc, symbols in files:
        base = path.rpartition("/")[2]
        lines.append(f"  {base:<34} {symbols:>4} symbols  {doc[:SUMMARY]}")
    return "\n".join(lines)


def file_card(db: MapStore, path: str, *, exported_only: bool = False) -> str:
    """One file's definitions, in source order, with line numbers to jump to."""
    symbols = db.symbols_in(path, exported_only=exported_only)
    docs = db.docs_for([path])
    if not symbols and path not in docs:
        return (
            f"{path} is not in the map — it may be unindexed, ignored, "
            "or in a language this installation does not parse."
        )

    lines = [f"{path}" + (f" — {docs[path]}" if docs.get(path) else "")]
    for symbol in symbols:
        where = f"{symbol.parent}." if symbol.parent else ""
        mark = "" if symbol.exported else " (private)"
        body = symbol.signature or f"{symbol.kind} {where}{symbol.name}"
        lines.append(f"  {symbol.line:>5}  {body}{mark}")
        if symbol.doc:
            lines.append(f"         {symbol.doc[:SUMMARY]}")

    imports = db.imports_of(path, internal_only=True)
    if imports:
        lines += ["", "  imports from this repo:"]
        lines += [f"    {resolved}" for _target, resolved in imports]
    importers = db.importers_of(path)
    if importers:
        lines += ["", f"  imported by {len(importers)}:"]
        lines += [f"    {importer}" for importer in importers[:12]]
    return "\n".join(lines)


def symbol_card(db: MapStore, name: str, *, kind: str = "", limit: int = 25) -> str:
    """Where a name is defined. The question grep answers with every mention."""
    hits = db.find(name, kind=kind, limit=limit)
    if not hits:
        exact = db.find(name, kind=kind, exact=True, limit=limit)
        if not exact:
            return f"no definition matching {name!r} in the map"
        hits = exact

    lines = [f"{len(hits)} definition" + ("s" if len(hits) != 1 else "") + f" matching {name!r}"]
    for path, symbol in hits:
        where = f"{symbol.parent}." if symbol.parent else ""
        lines.append(f"  {path}:{symbol.line}  {symbol.kind} {where}{symbol.name}")
        if symbol.signature:
            lines.append(f"      {symbol.signature}")
        if symbol.doc:
            lines.append(f"      {symbol.doc[:SUMMARY]}")
    return "\n".join(lines)


def _thousands(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _ago(when: float, *, now: float = 0.0) -> str:
    """How long ago, in the roughest unit that is still true.

    A map's age is the one thing about it a reader has to judge for
    themselves --- an index from last month is not wrong, but it is not
    evidence either --- so this is never rounded up into sounding fresher.
    """
    if when <= 0:
        return "never"
    seconds = max(0.0, (now or time.time()) - when)
    for size, unit in ((86400.0, "day"), (3600.0, "hour"), (60.0, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}" + ("s" if count != 1 else "") + " ago"
    return "just now"
