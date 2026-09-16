"""Where workflows live: one TOML file each, under the config directory.

TOML because everything else here is TOML --- the config, the MCP manifests ---
and because a workflow is meant to be readable, diffable and checked into a
repo next to the code it operates on.

The filename is the name. A ``name`` key is written into the file as well, for
anyone reading it on its own, but the stem wins on load: a file copied to a new
name should become that workflow, not silently keep the old one and overwrite
its neighbour on the next save.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from altus.config.loader import config_dir
from altus.core.errors import ConfigError
from altus.workflow.models import Workflow, valid_slug

SUFFIX = ".toml"


def workflows_dir(settings: Any = None) -> Path:
    """``[workflow] dir``, or ``<config>/workflows``."""
    override = getattr(settings, "dir", "") if settings is not None else ""
    return Path(override).expanduser() if override else config_dir() / "workflows"


def path_for(name: str, settings: Any = None) -> Path:
    """The file a name maps to, refusing anything that could leave the directory.

    Checked here rather than only at the model, because this is the function
    that turns a string into a filesystem path --- and one of its callers is a
    tool the *model* drives, which makes ``../../.ssh/authorized_keys`` a
    thing someone will eventually try.
    """
    if not valid_slug(name):
        raise ConfigError(
            f"{name!r} is not a usable workflow name: lowercase letters, digits, "
            "'-' and '_' only, starting with a letter or digit"
        )
    return workflows_dir(settings) / f"{name}{SUFFIX}"


def list_workflows(settings: Any = None) -> list[str]:
    directory = workflows_dir(settings)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob(f"*{SUFFIX}") if valid_slug(p.stem))


def render(workflow: Workflow) -> str:
    """The TOML text for a workflow. Also what the approval prompt shows."""
    payload: dict[str, Any] = {"name": workflow.name}
    if workflow.description:
        payload["description"] = workflow.description
    steps: list[dict[str, Any]] = []
    for step in workflow.steps:
        # exclude_defaults keeps an empty `needs` or `args` out of the file, so
        # a hand-written workflow stays as short as the author wrote it.
        rest = step.model_dump(mode="json", exclude_defaults=True)
        rest.pop("id", None)
        rest.pop("kind", None)
        steps.append({"id": step.id, "kind": step.kind, **rest})
    payload["steps"] = steps
    return tomli_w.dumps(payload)


def load(name: str, settings: Any = None) -> Workflow:
    path = path_for(name, settings)
    if not path.exists():
        raise ConfigError(f"no workflow called {name!r}. Run /workflow list to see them.")
    return parse(path.read_text(encoding="utf-8"), name=name, where=str(path))


def parse(text: str, *, name: str, where: str = "") -> Workflow:
    """Text to workflow, with the filename winning over any ``name`` inside."""
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{where or name} is not valid TOML: {exc}") from exc
    payload["name"] = name
    try:
        return Workflow.model_validate(payload)
    except Exception as exc:
        raise ConfigError(f"{where or name} is not a usable workflow: {exc}") from exc


def save(workflow: Workflow, settings: Any = None) -> Path:
    path = path_for(workflow.name, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(workflow), encoding="utf-8")
    return path


def delete(name: str, settings: Any = None) -> bool:
    path = path_for(name, settings)
    if not path.exists():
        return False
    path.unlink()
    return True
