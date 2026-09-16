"""The audit trail: one JSONL file per run, written as it happens.

Written eagerly rather than at the end, for the same reason ``storage.sessions``
does it: the runs you most want a record of are the ones that were interrupted,
and a record assembled at the end is exactly the record those runs never get.

Under ``data_dir()/runs``, not the config directory --- a run record is
something that happened, not something you configured, and nobody wants their
config directory growing a file every time they run a workflow.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from altus.config.loader import data_dir
from altus.workflow.events import parse_event

log = logging.getLogger(__name__)


def runs_dir() -> Path:
    return data_dir() / "runs"


class RunRecorder:
    """Appends one JSON object per event. Never raises into the engine.

    A failure to write the record must not take down the run it is recording:
    losing the trail is bad, and losing a half-applied change because the disk
    was full is worse.
    """

    def __init__(self, run_id: str, root: Path | None = None) -> None:
        self.root = root or runs_dir()
        self.path = self.root / f"{run_id}.jsonl"
        self.broken = False

    def write(self, event: Any) -> None:
        if self.broken:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json")) + "\n")
        except OSError as exc:
            self.broken = True
            log.warning("run record for %s could not be written: %s", self.path.stem, exc)


def list_runs(root: Path | None = None, limit: int = 20) -> list[str]:
    """Run ids, newest first."""
    directory = root or runs_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [path.stem for path in files[:limit]]


def read_run(run_id: str, root: Path | None = None) -> list[Any]:
    """Every event of one run, in order. Corrupt lines are skipped, not fatal."""
    path = (root or runs_dir()) / f"{run_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no run called {run_id}")
    return list(_events(path))


def _events(path: Path) -> Iterator[Any]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield parse_event(json.loads(line))
        except Exception:
            # A truncated final line is the normal shape of an interrupted run.
            log.warning("skipping unreadable line %d in %s", number, path)


def summarise(events: list[Any]) -> str:
    """One line per run, for a list. Says "interrupted" when nothing closed it."""
    from altus.workflow.events import RunFinished, RunStarted

    start = next((e for e in events if isinstance(e, RunStarted)), None)
    # The *last* ending, because a resumed run has two: the park and whatever
    # happened after somebody approved it.
    end = next((e for e in reversed(events) if isinstance(e, RunFinished)), None)
    if start is None:
        return "unreadable"
    when = start.started
    if end is None:
        # No RunFinished line means the process went away mid-run. Saying
        # "completed" here would be the record lying about the one case it
        # exists for.
        return f"{when}  {start.workflow:<20} interrupted"
    if end.state == "parked":
        return f"{when}  {start.workflow:<20} parked — {end.detail}"
    return f"{when}  {start.workflow:<20} {end.state} — {end.ran} steps in {end.seconds}s"


def is_parked(events: list[Any]) -> bool:
    """Waiting for a person, rather than finished.

    From the last ending, so a run that parked and was then resumed and
    completed is not offered for approval a second time.
    """
    from altus.workflow.events import RunFinished

    end = next((e for e in reversed(events) if isinstance(e, RunFinished)), None)
    return end is not None and end.state == "parked"


def parked_runs(root: Path | None = None, limit: int = 50) -> list[tuple[str, str]]:
    """``(run_id, one-line summary)`` for every run waiting on a person."""
    found: list[tuple[str, str]] = []
    for run_id in list_runs(root, limit):
        try:
            events = read_run(run_id, root)
        except OSError:
            continue
        if is_parked(events):
            found.append((run_id, summarise(events)))
    return found
