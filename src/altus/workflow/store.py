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
TEMPLATES = Path(__file__).parent / "templates"

#: What a step's keys look like when read top to bottom: what it is, what it
#: waits for, then how it behaves. Anything unlisted sorts between `args` and
#: the trailing pair, alphabetically, so a new field cannot silently land in
#: the middle of the identity block.
KEY_ORDER = {
    "id": 0,
    "kind": 1,
    "needs": 2,
    "tool": 10,
    "args": 11,
    "prompt": 12,
    "tools": 13,
    "message": 14,
    "wait": 90,
    "on_error": 91,
}


#: The same idea for a `[[triggers]]` block: what it is, then how often, then
#: what it looks at.
TRIGGER_ORDER = {"kind": 0, "every": 1, "tool": 10, "args": 11, "into": 12}


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
    if workflow.parallel != 1:
        payload["parallel"] = workflow.parallel
    if workflow.inputs:
        # Before [[steps]]: TOML puts every table after the scalars that follow
        # it, so an `inputs` table written later would swallow the step array.
        payload["inputs"] = {
            name: spec.model_dump(mode="json", exclude_defaults=True)
            for name, spec in workflow.inputs.items()
        }
    out = tomli_w.dumps(payload).rstrip()
    for trigger in workflow.triggers:
        # Before the steps, because once a [[steps]] block is open every later
        # key belongs to it.
        entry = trigger.model_dump(mode="json", exclude_defaults=True)
        entry.setdefault("kind", trigger.kind)
        out += "\n\n" + _table("triggers", entry, TRIGGER_ORDER)
    for step in workflow.steps:
        # exclude_defaults keeps an empty `needs` or `args` out of the file, so
        # a hand-written workflow stays as short as the author wrote it.
        rest = step.model_dump(mode="json", exclude_defaults=True)
        rest.pop("id", None)
        rest.pop("kind", None)
        out += "\n\n" + _step_toml({"id": step.id, "kind": step.kind, **rest})
    return out + "\n"


def fingerprint(workflow: Workflow) -> str:
    """A short hash of the workflow as written.

    Recorded when a run starts so a parked run can only be resumed against the
    file it was planned from. Taken over ``render`` rather than over the file's
    bytes so a comment or a reordered key does not invalidate a run, while any
    change to what would actually happen does.
    """
    import hashlib

    return hashlib.sha256(render(workflow).encode("utf-8")).hexdigest()[:16]


def _step_toml(entry: dict[str, Any]) -> str:
    """One ``[[steps]]`` block, written out rather than left to tomli_w.

    tomli_w decides between a block and a one-line inline table by a heuristic
    about what the values happen to contain, so the same workflow could render
    either way depending on whether a step had a `needs` entry. This is the
    format people read, diff and commit, and it should not shift under them.
    """
    return _table("steps", entry, KEY_ORDER)


def _table(name: str, entry: dict[str, Any], order: dict[str, int]) -> str:
    lines = [f"[[{name}]]"]
    ordered = sorted(entry.items(), key=lambda item: (order.get(item[0], 50), item[0]))
    lines += [f"{key} = {_value(value)}" for key, value in ordered]
    return "\n".join(lines)


def _value(value: Any) -> str:
    """One TOML value, inline. The value space is whatever JSON allows,
    because that is what a tool's arguments are."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{key} = {_value(item)}" for key, item in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if value is None:
        # Nothing in a workflow should reach here --- every optional field is
        # excluded by exclude_defaults --- and TOML has no null, so an empty
        # string is the only representable thing.
        return '""'
    return _value(str(value))


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


# ------------------------------------------------------------------ templates


def templates() -> list[str]:
    """The workflows Altus ships as starting points."""
    return sorted(path.stem for path in TEMPLATES.glob(f"*{SUFFIX}"))


def template(name: str) -> Workflow:
    """One shipped template, parsed. Raises if there is no such thing."""
    path = TEMPLATES / f"{name}{SUFFIX}"
    if not valid_slug(name) or not path.is_file():
        raise ConfigError(f"no template called {name!r}. Available: {', '.join(templates())}")
    return parse(path.read_text(encoding="utf-8"), name=name, where=str(path))


def template_text(name: str) -> str:
    """The file as written, comments and all --- which is most of its value."""
    path = TEMPLATES / f"{name}{SUFFIX}"
    if not valid_slug(name) or not path.is_file():
        raise ConfigError(f"no template called {name!r}. Available: {', '.join(templates())}")
    return path.read_text(encoding="utf-8")


def copy_template(name: str, into: str, settings: Any = None) -> Path:
    """Copy a template to a new workflow, keeping its prose.

    Copied as text rather than parsed and re-rendered, because the comments
    explaining *why* each step is there are the part a reader needs most and
    a round trip through the model would drop every one of them.
    """
    text = template_text(name)
    path = path_for(into, settings)
    if path.exists():
        raise ConfigError(f"{into} already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The name lives in the file too, and the stem wins on load --- but leaving
    # the template's name inside a copy is confusing to read.
    path.write_text(text.replace(f'name = "{name}"', f'name = "{into}"', 1), encoding="utf-8")
    return path
