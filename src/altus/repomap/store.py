"""The index on disk: SQLite, under the data directory, one file per repository.

Two choices worth defending.

**Not in the working tree.** A map of a repository is a tempting thing to put
*in* that repository, and it would be wrong: Altus edits that tree, so the
index would show up in diffs, need a gitignore entry, and eventually be
committed stale by somebody in a hurry. Derived data belongs with the other
derived data, beside the session transcripts.

**SQLite rather than a JSON blob.** The questions are lookups --- this name,
this kind, who imports this path --- and an index answers them without loading
50,000 symbols into memory to filter them. It is also the standard library, so
this costs no dependency.

The schema is a *cache*: every row can be recomputed from the tree it came
from. That is why ``SCHEMA_VERSION`` is enforced by deleting the database
rather than by writing migrations. A migration is for data you cannot get back.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from altus.config.loader import data_dir
from altus.repomap.models import Extracted, FileFacts, Symbol

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE files (
    path     TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    digest   TEXT NOT NULL,
    lines    INTEGER NOT NULL,
    doc      TEXT NOT NULL DEFAULT '',
    symbols  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE symbols (
    path      TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    line      INTEGER NOT NULL,
    end_line  INTEGER NOT NULL DEFAULT 0,
    parent    TEXT NOT NULL DEFAULT '',
    exported  INTEGER NOT NULL DEFAULT 1,
    signature TEXT NOT NULL DEFAULT '',
    doc       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX symbols_name ON symbols(name);
CREATE INDEX symbols_path ON symbols(path);
CREATE INDEX symbols_lower ON symbols(name COLLATE NOCASE);

CREATE TABLE imports (
    path     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    target   TEXT NOT NULL,
    resolved TEXT NOT NULL DEFAULT '',
    line     INTEGER NOT NULL
);
CREATE INDEX imports_path ON imports(path);
CREATE INDEX imports_resolved ON imports(resolved);

CREATE TABLE churn (
    path      TEXT PRIMARY KEY,
    commits   INTEGER NOT NULL DEFAULT 0,
    authors   INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL DEFAULT ''
);
"""


def map_path(root: Path) -> Path:
    """Where the index for ``root`` lives.

    The directory name goes in for a human reading ``ls``; the hash of the
    absolute path is what actually keys it, because two checkouts of the same
    repository in different places are two different maps and must not share a
    file.
    """
    resolved = root.resolve()
    tag = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in resolved.name) or "repo"
    return data_dir() / "maps" / f"{safe}-{tag}.db"


class MapStore:
    """One repository's index. Synchronous: callers put it on a thread.

    SQLite is fast enough that making this async would mean an event loop
    hop per query to save nothing, and every caller here already has a
    ``to_thread`` boundary for the file reads it does alongside.
    """

    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = self._open()

    def _open(self) -> sqlite3.Connection:
        fresh = not self.path.exists()
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        if not fresh and self._version(db) != SCHEMA_VERSION:
            # Derived data: the cheapest correct migration is to throw it away
            # and let the next reconcile rebuild. Worth being loud about,
            # because the rebuild is the slow pass.
            log.info("repo map schema changed, rebuilding %s", self.path)
            db.close()
            self.path.unlink(missing_ok=True)
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            fresh = True
        if fresh:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("root", str(self.root)))
            db.commit()
        return db

    @staticmethod
    def _version(db: sqlite3.Connection) -> int:
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError:
            return -1
        try:
            return int(row["value"]) if row else -1
        except TypeError, ValueError:
            return -1

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> MapStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    @property
    def built_at(self) -> float:
        try:
            return float(self.get_meta("built_at", "0"))
        except ValueError:
            return 0.0

    def mark_built(self) -> None:
        self.set_meta("built_at", f"{time.time():.0f}")

    # ----------------------------------------------------------------- files

    def known_files(self) -> dict[str, FileFacts]:
        """Every indexed file, keyed by path. Read once per reconcile pass."""
        rows = self._db.execute(
            "SELECT path, language, size, mtime_ns, digest, lines, doc, symbols FROM files"
        )
        return {
            str(row["path"]): FileFacts(
                path=str(row["path"]),
                language=str(row["language"]),
                size=int(row["size"]),
                mtime_ns=int(row["mtime_ns"]),
                digest=str(row["digest"]),
                lines=int(row["lines"]),
                doc=str(row["doc"]),
                symbols=int(row["symbols"]),
            )
            for row in rows
        }

    def file_count(self) -> int:
        row = self._db.execute("SELECT count(*) AS n FROM files").fetchone()
        return int(row["n"]) if row else 0

    def symbol_count(self) -> int:
        row = self._db.execute("SELECT count(*) AS n FROM symbols").fetchone()
        return int(row["n"]) if row else 0

    def languages(self) -> list[tuple[str, int]]:
        rows = self._db.execute(
            "SELECT language, count(*) AS n FROM files GROUP BY language ORDER BY n DESC, language"
        )
        return [(str(row["language"]), int(row["n"])) for row in rows]

    def all_files(self) -> list[tuple[str, str, int, str, int]]:
        """``(path, language, lines, doc, symbols)`` for everything indexed."""
        rows = self._db.execute(
            "SELECT path, language, lines, doc, symbols FROM files ORDER BY path"
        )
        return [
            (
                str(row["path"]),
                str(row["language"]),
                int(row["lines"]),
                str(row["doc"]),
                int(row["symbols"]),
            )
            for row in rows
        ]

    def files_under(self, directory: str) -> list[tuple[str, str, int, str, int]]:
        """The same, for one directory and everything below it.

        ``.`` means the whole repository. A prefix match needs the trailing
        slash or ``src/alt`` would also match ``src/altus``.
        """
        prefix = "" if directory in ("", ".") else directory.rstrip("/") + "/"
        return [row for row in self.all_files() if row[0].startswith(prefix)]

    def top_exports(self, directory: str, *, limit: int = 6) -> list[Symbol]:
        """The public definitions a directory is most likely known for.

        Types before functions, and longer definitions before shorter ones:
        with nothing else to go on, the biggest public class in a package is a
        better one-line answer to "what is this" than its first helper.
        """
        prefix = "" if directory in ("", ".") else directory.rstrip("/") + "/"
        rows = self._db.execute(
            "SELECT name, kind, line, end_line, parent, exported, signature, doc "
            "FROM symbols WHERE exported = 1 AND parent = '' AND path LIKE ? "
            "ORDER BY CASE kind WHEN 'class' THEN 0 WHEN 'type' THEN 0 "
            "WHEN 'interface' THEN 0 ELSE 1 END, (end_line - line) DESC, name LIMIT ?",
            (f"{prefix}%", limit),
        )
        return [_symbol(row) for row in rows]

    def touch(self, facts: FileFacts) -> None:
        """Record that a file's stat changed but its content did not.

        Without this the cheap gate never closes: a file whose mtime moved
        would be re-hashed on every single pass for the rest of its life.
        """
        self._db.execute(
            "UPDATE files SET size = ?, mtime_ns = ? WHERE path = ?",
            (facts.size, facts.mtime_ns, facts.path),
        )

    def put(self, facts: FileFacts, extracted: Extracted) -> None:
        """Replace one file and everything derived from it, as one unit.

        Delete-then-insert rather than a diff: a file's symbols are cheap to
        rewrite and expensive to reconcile correctly, and a half-updated file
        is an index that lies about a line number.
        """
        self._db.execute("DELETE FROM files WHERE path = ?", (facts.path,))
        self._db.execute(
            "INSERT INTO files (path, language, size, mtime_ns, digest, lines, doc, symbols) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                facts.path,
                facts.language,
                facts.size,
                facts.mtime_ns,
                facts.digest,
                facts.lines,
                extracted.doc,
                len(extracted.symbols),
            ),
        )
        self._db.executemany(
            "INSERT INTO symbols "
            "(path, name, kind, line, end_line, parent, exported, signature, doc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    facts.path,
                    symbol.name,
                    symbol.kind,
                    symbol.line,
                    symbol.end_line,
                    symbol.parent,
                    int(symbol.exported),
                    symbol.signature,
                    symbol.doc,
                )
                for symbol in extracted.symbols
            ],
        )
        self._db.executemany(
            "INSERT INTO imports (path, target, resolved, line) VALUES (?, ?, ?, ?)",
            [(facts.path, imp.target, imp.resolved, imp.line) for imp in extracted.imports],
        )

    def forget(self, paths: Iterable[str]) -> int:
        """Drop files that are no longer on disk. Cascades to their rows."""
        listed = list(paths)
        self._db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in listed])
        return len(listed)

    def commit(self) -> None:
        self._db.commit()

    # --------------------------------------------------------------- queries

    def find(
        self,
        name: str,
        *,
        kind: str = "",
        exact: bool = False,
        limit: int = 50,
    ) -> list[tuple[str, Symbol]]:
        """Definitions matching ``name``, as ``(path, symbol)``.

        Case-insensitive and substring by default, because the caller asking
        is usually working from a half-remembered name. Exported definitions
        sort first: when ``Config`` matches a public class and a private
        helper, the public one is the answer being looked for.
        """
        clause = "name = ? COLLATE NOCASE" if exact else "name LIKE ? COLLATE NOCASE"
        needle = name if exact else f"%{name}%"
        params: list[object] = [needle]
        if kind:
            clause += " AND kind = ?"
            params.append(kind)
        params.append(limit)
        rows = self._db.execute(
            "SELECT path, name, kind, line, end_line, parent, exported, signature, doc "
            f"FROM symbols WHERE {clause} "
            "ORDER BY exported DESC, length(name), name, path LIMIT ?",
            params,
        )
        return [(str(row["path"]), _symbol(row)) for row in rows]

    def symbols_in(self, path: str, *, exported_only: bool = False) -> list[Symbol]:
        clause = " AND exported = 1" if exported_only else ""
        rows = self._db.execute(
            "SELECT name, kind, line, end_line, parent, exported, signature, doc "
            f"FROM symbols WHERE path = ?{clause} ORDER BY line",
            (path,),
        )
        return [_symbol(row) for row in rows]

    def importers_of(self, path: str) -> list[str]:
        """Files that import ``path``. The question grep answers badly."""
        rows = self._db.execute(
            "SELECT DISTINCT path FROM imports WHERE resolved = ? ORDER BY path", (path,)
        )
        return [str(row["path"]) for row in rows]

    def imports_of(self, path: str, *, internal_only: bool = False) -> list[tuple[str, str]]:
        clause = " AND resolved != ''" if internal_only else ""
        rows = self._db.execute(
            f"SELECT target, resolved FROM imports WHERE path = ?{clause} ORDER BY line",
            (path,),
        )
        return [(str(row["target"]), str(row["resolved"])) for row in rows]

    def fan_in(self, limit: int = 20) -> list[tuple[str, int]]:
        """The most-imported files in the repository.

        A reasonable definition of "where to start reading": the modules the
        rest of the tree depends on, ranked by how much of it does.
        """
        rows = self._db.execute(
            "SELECT resolved AS path, count(DISTINCT path) AS n FROM imports "
            "WHERE resolved != '' GROUP BY resolved ORDER BY n DESC, resolved LIMIT ?",
            (limit,),
        )
        return [(str(row["path"]), int(row["n"])) for row in rows]

    def directories(self) -> list[tuple[str, int, int]]:
        """``(directory, files, symbols)`` for every directory holding code."""
        rows = self._db.execute(
            "SELECT path, symbols FROM files",
        )
        tally: dict[str, list[int]] = {}
        for row in rows:
            parent = str(row["path"]).rpartition("/")[0] or "."
            entry = tally.setdefault(parent, [0, 0])
            entry[0] += 1
            entry[1] += int(row["symbols"])
        return sorted(
            ((name, counts[0], counts[1]) for name, counts in tally.items()),
            key=lambda item: item[0],
        )

    def docs_for(self, paths: Sequence[str]) -> dict[str, str]:
        if not paths:
            return {}
        marks = ", ".join("?" for _ in paths)
        rows = self._db.execute(
            f"SELECT path, doc FROM files WHERE path IN ({marks})", tuple(paths)
        )
        return {str(row["path"]): str(row["doc"]) for row in rows}

    def resolve_edges(self, resolver: dict[str, str]) -> int:
        """Fill ``imports.resolved`` from a target -> path map.

        Run after the walk rather than during it, because resolving an import
        needs the whole file set and an extractor is handed one file.
        """
        rows = self._db.execute("SELECT rowid, path, target FROM imports").fetchall()
        updates = []
        for row in rows:
            target = _resolve(str(row["target"]), str(row["path"]), resolver)
            if target:
                updates.append((target, int(row["rowid"])))
        self._db.executemany("UPDATE imports SET resolved = ? WHERE rowid = ?", updates)
        self._db.commit()
        return len(updates)

    # ----------------------------------------------------------------- churn

    def put_churn(self, rows: Iterable[tuple[str, int, int, str]]) -> None:
        self._db.executemany(
            "INSERT INTO churn (path, commits, authors, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET commits = excluded.commits, "
            "authors = excluded.authors, last_seen = excluded.last_seen",
            list(rows),
        )
        self._db.commit()

    def hotspots(self, limit: int = 20) -> list[tuple[str, int, int, str]]:
        """Files by commit count, restricted to files that still exist.

        The join matters: git history remembers files the index does not, and
        a list of hotspots pointing at deleted paths is a list of dead ends.
        """
        rows = self._db.execute(
            "SELECT c.path, c.commits, c.authors, c.last_seen FROM churn c "
            "JOIN files f ON f.path = c.path "
            "ORDER BY c.commits DESC, c.path LIMIT ?",
            (limit,),
        )
        return [
            (str(row["path"]), int(row["commits"]), int(row["authors"]), str(row["last_seen"]))
            for row in rows
        ]


def _symbol(row: sqlite3.Row) -> Symbol:
    return Symbol(
        name=str(row["name"]),
        kind=str(row["kind"]),
        line=int(row["line"]),
        end_line=int(row["end_line"]),
        parent=str(row["parent"]),
        exported=bool(row["exported"]),
        signature=str(row["signature"]),
        doc=str(row["doc"]),
    )


def _resolve(target: str, importer: str, resolver: dict[str, str]) -> str:
    """One import target to a repository path, or "" when it points outside.

    Relative targets are resolved against the importing file's own directory
    --- both the Python dotted form and the ``./`` form JavaScript uses ---
    because in both languages the same written target means different files
    depending on where it was written.
    """
    if target.startswith("."):
        return _relative(target, importer, resolver)
    return resolver.get(target, "")


def _relative(target: str, importer: str, resolver: dict[str, str]) -> str:
    directory = importer.rpartition("/")[0]
    if target.startswith("./") or target.startswith("../"):
        parts = directory.split("/") if directory else []
        for piece in target.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(piece)
        return resolver.get("/".join(parts), "")

    # Python: one leading dot is this package, two is its parent, and the
    # remainder is a dotted path below whichever that is.
    dots = len(target) - len(target.lstrip("."))
    parts = directory.split("/") if directory else []
    for _ in range(dots - 1):
        if parts:
            parts.pop()
    rest = target[dots:]
    if rest:
        parts += rest.split(".")
    return resolver.get("/".join(parts), "")


def store_for(root: Path) -> MapStore:
    """The index for a repository root, created on first use."""
    return MapStore(map_path(root), root)
