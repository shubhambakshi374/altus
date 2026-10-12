"""The walk, and the reconcile that makes a second run cheap.

Everything here is synchronous on purpose. SQLite connections belong to the
thread that opened them, and the parsing is CPU work that an event loop cannot
overlap anyway, so the whole pass runs on one thread and the callers that have
an event loop put it there with ``asyncio.to_thread``. An async walker would
buy nothing and would add a way to use a connection from the wrong thread.

**Containment is not re-implemented.** Every candidate goes through
``Workspace.is_allowed``, the same check the filesystem tools use, before it is
opened --- a new walker that did its own path arithmetic would be a second
answer to a question that already has one, and the one it would get wrong is
the secret denylist.

**Invalidation is by content, with stat as a gate.** ``size`` and ``mtime_ns``
decide whether to read a file at all; the sha256 of its bytes decides whether
to parse it. Both are needed: stat alone re-parses the world after a checkout
that changed nothing, and hashing alone reads the world on every pass.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from altus.repomap.languages import Extractor, extractor_for, language_of
from altus.repomap.models import Extracted, FileFacts, IndexStats
from altus.repomap.store import MapStore, store_for
from altus.tools._ignore import IgnoreFilter
from altus.workspace import Workspace

log = logging.getLogger(__name__)

#: Files larger than this are counted and not parsed. A 4 MB JavaScript bundle
#: is one symbol table nobody wants and several seconds of parsing to get it.
MAX_FILE_BYTES = 512 * 1024

#: A ceiling on the walk, so pointing the map at a monorepo degrades into a
#: partial map with a note rather than a session that appears to have hung.
MAX_FILES = 20_000

#: Directories never worth descending into, whatever the ignore files say.
#: ``.git`` is handled by IgnoreFilter; these are the ones a repository
#: routinely fails to ignore and nobody wants indexed.
SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "vendor",
        "target",
        "dist",
        "build",
        ".tox",
        ".next",
        ".gradle",
        "site-packages",
    }
)


def reconcile(
    workspace: Workspace,
    *,
    store: MapStore | None = None,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    full: bool = False,
) -> IndexStats:
    """Bring the index for ``workspace`` up to date with the tree.

    ``full`` re-parses every file regardless of hash. Only useful when the
    extractors themselves changed --- installing the extra, or upgrading a
    grammar --- which the stored hashes cannot possibly notice.
    """
    started = time.monotonic()
    own = store is None
    db = store or store_for(workspace.root)
    try:
        return _reconcile(workspace, db, max_files, max_file_bytes, full, started)
    finally:
        if own:
            db.close()


def _reconcile(
    workspace: Workspace,
    db: MapStore,
    max_files: int,
    max_file_bytes: int,
    full: bool,
    started: float,
) -> IndexStats:
    stats = IndexStats()
    known = db.known_files()
    seen: set[str] = set()
    extractors: dict[str, Extractor | None] = {}

    for path, relative, stat in _walk(workspace, max_files):
        stats.scanned += 1
        seen.add(relative)
        language = language_of(relative)
        if language not in extractors:
            extractors[language] = extractor_for(language)
        extractor = extractors[language]
        if extractor is None:
            stats.unsupported[language] = stats.unsupported.get(language, 0) + 1
            continue
        if stat.st_size > max_file_bytes:
            stats.skipped += 1
            continue

        before = known.get(relative)
        if (
            not full
            and before is not None
            and before.size == stat.st_size
            and before.mtime_ns == stat.st_mtime_ns
        ):
            stats.unchanged += 1
            continue

        try:
            raw = path.read_bytes()
        except OSError as exc:
            log.debug("repo map could not read %s: %s", relative, exc)
            stats.failed.append(relative)
            continue

        digest = hashlib.sha256(raw).hexdigest()
        facts = FileFacts(
            path=relative,
            language=language,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            digest=digest,
            lines=raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0),
        )
        if not full and before is not None and before.digest == digest:
            # Same bytes, new mtime: a checkout, a touch, a formatter that
            # changed nothing. Record the new stat so the cheap gate closes
            # again, and do not parse.
            db.touch(facts)
            stats.unchanged += 1
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            stats.failed.append(relative)
            continue
        try:
            extracted = extractor.extract(text, relative)
        except (SyntaxError, ValueError, RecursionError, UnicodeDecodeError) as exc:
            # A repository mid-refactor always contains a file that does not
            # parse. Half a map is worth more than no map, so this is recorded
            # and the walk continues.
            log.debug("repo map could not parse %s: %s", relative, exc)
            stats.failed.append(relative)
            db.put(facts, Extracted())
            continue

        db.put(facts, extracted)
        stats.parsed += 1

    stats.removed = db.forget(path for path in known if path not in seen)
    db.commit()
    db.resolve_edges(_resolver(seen))
    db.mark_built()
    stats.seconds = time.monotonic() - started
    return stats


def _walk(workspace: Workspace, max_files: int) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Yield ``(path, relative, stat)`` for every file worth considering.

    Pruned top-down: an ignored directory is never descended into, which is
    the difference between a second and a minute on a repository with a
    ``node_modules``.
    """
    ignores = IgnoreFilter(workspace)
    root = workspace.root
    yielded = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        here = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIRS and not ignores.is_ignored(here / name)
        )
        for name in sorted(filenames):
            if not language_of(name):
                continue
            path = here / name
            if ignores.is_ignored(path) or not workspace.is_allowed(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            yield path, path.relative_to(root).as_posix(), stat
            yielded += 1
            if yielded >= max_files:
                return


def _resolver(paths: set[str]) -> dict[str, str]:
    """A map from written import target to repository path.

    Two key families share this dictionary, and they cannot collide: dotted
    module names always contain a ``.`` and extensionless paths always contain
    a ``/`` (or are a single bare name, where both spellings mean the same
    file anyway).
    """
    packages = {
        path.rpartition("/")[0]
        for path in paths
        if path.endswith(("/__init__.py", "/__init__.pyi"))
    }
    resolver: dict[str, str] = {}
    for path in sorted(paths):
        stem = path[: path.rindex(".")] if "." in path.rpartition("/")[2] else ""
        if not stem:
            continue
        resolver[stem] = path

        directory, _, base = stem.rpartition("/")
        if base in ("index", "mod", "__init__"):
            # `./util` meaning `util/index.ts`, Rust's `mod.rs`, and a Python
            # package imported by its own name.
            resolver.setdefault(directory, path)
        if path.endswith((".py", ".pyi")):
            module = _module_name(stem, packages)
            if module:
                resolver.setdefault(module, path)
    return resolver


def _module_name(stem: str, packages: set[str]) -> str:
    """The dotted name Python would import ``stem`` by.

    Found the way the interpreter finds it: climb while the directory above is
    a package, and whatever sits above the topmost package is the source root.
    That is what makes a ``src/`` layout resolve without anybody having
    configured ``src/``.
    """
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return ""
    first = len(parts) - 1
    while first > 0 and "/".join(parts[:first]) in packages:
        first -= 1
    return ".".join(parts[first:])
