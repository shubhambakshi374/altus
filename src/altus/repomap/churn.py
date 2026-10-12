"""Where the work has been, from git history.

A symbol table says what exists; it cannot say what matters. The two
questions a person actually opens a strange repository with --- *what is the
important code* and *what is being worked on right now* --- are answered by
different things, and only the second is in the history.

So this is a separate pass with a separate invalidation. Content hashes cannot
notice that HEAD moved, and re-walking a tree because somebody committed would
be wrong; equally, a commit changes churn for files whose bytes did not move at
all. The pass is keyed on the commit it was computed at, and skipped when that
has not changed.

Read-only git, with every failure degrading to "no churn data" rather than to
an error. A repository with no history, a shallow clone, a directory that is
not a checkout at all --- the map is still a map without this.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from altus.repomap.store import MapStore

log = logging.getLogger(__name__)

TIMEOUT = 20.0

#: Commits to read back. A full history on a large repository is tens of
#: thousands of commits and minutes of parsing, and the answer barely moves:
#: churn is a question about the recent past, and code nobody has touched in
#: 2,000 commits is not a hotspot however busy it once was.
COMMITS = 2_000

#: Marks a commit header, so a path can never be mistaken for one. It has to
#: be a byte that cannot plausibly appear in an author name or a path --- a
#: `|` or a tab can, and an author who set their name to one would be able to
#: forge a record boundary. NUL would be the obvious choice and is not
#: available: this travels through `execve` as part of an argument, and argv
#: strings are NUL-terminated, so a NUL in one is rejected before git runs.
MARK = "\x01"


async def head(root: Path) -> str:
    """The current commit, or "" when this is not a usable checkout."""
    code, out = await _git(root, "rev-parse", "HEAD")
    return out.strip() if code == 0 else ""


async def update(db: MapStore, root: Path, *, commits: int = COMMITS) -> int:
    """Refresh churn when HEAD has moved. Returns the number of files touched.

    Returning 0 is the ordinary case on a repeat call and is not a failure:
    the stored answer is still correct for the commit it was taken at.
    """
    current = await head(root)
    if not current:
        return 0
    if current == db.get_meta("churn_head"):
        return 0

    rows = await collect(root, commits=commits)
    if rows:
        db.put_churn(rows)
    db.set_meta("churn_head", current)
    db.set_meta("churn_commits", str(commits))
    return len(rows)


async def collect(root: Path, *, commits: int = COMMITS) -> list[tuple[str, int, int, str]]:
    """``(path, commits, authors, last_seen)`` for every file in the history.

    One ``git log`` rather than one call per file: the per-file version is
    correct and takes minutes on a repository of any size, and this is a
    single pass over output git already has to produce.

    Merges are excluded. A merge commit touches every file both sides touched,
    so counting them makes the files near a long-running branch look like the
    busiest code in the repository when nobody edited them at all.
    """
    code, out = await _git(
        root,
        "log",
        f"-n{commits}",
        "--no-merges",
        f"--format={MARK}%H{MARK}%an{MARK}%as",
        "--name-only",
    )
    if code != 0:
        return []

    counts: dict[str, int] = {}
    authors: dict[str, set[str]] = {}
    last: dict[str, str] = {}
    author = ""
    date = ""
    for line in out.splitlines():
        if line.startswith(MARK):
            _, _sha, author, date = line.split(MARK, 3)
            continue
        path = line.strip()
        if not path:
            continue
        counts[path] = counts.get(path, 0) + 1
        authors.setdefault(path, set()).add(author)
        # git log is newest first, so the first sighting is the latest one.
        last.setdefault(path, date)

    return [
        (path, count, len(authors.get(path, ())), last.get(path, ""))
        for path, count in counts.items()
    ]


async def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except (OSError, TimeoutError) as exc:
        log.debug("churn: git %s failed: %s", args[0] if args else "", exc)
        return 1, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace")
