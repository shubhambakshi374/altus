"""How one step's output reaches the next.

``${inventory}`` anywhere in a later step's arguments, prompt or message is
replaced with what the step called ``inventory`` produced. That is the whole
mechanism --- one substitution, no expressions, no indexing into structure.

An expression language was the alternative and it is the wrong trade here.
Every expression language grows conditionals, and a workflow whose shape
depends on values computed at run time cannot have its blast radius worked out
before it runs --- which is the one property this design has spent two
increments buying. Steps compose by passing text; anything cleverer is an
``agent`` step's job, where the gate is already watching.

The rule that makes it safe is in ``validate``: a step may only reference
something it actually depends on. ``${x}`` without ``x`` in the transitive
closure of ``needs`` is fatal, because nothing would have guaranteed ``x`` ran.
"""

from __future__ import annotations

import re
from typing import Any

#: A step id, or ``inputs.<name>``. Step ids cannot contain a dot --- the slug
#: rule forbids it --- so the two forms can never be confused for each other.
REF = re.compile(r"\$\{([a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)?)\}")

MAX_SUBSTITUTED = 20_000
"""A step's output is capped before it is pasted into the next step's
arguments. Without this, one `k8s_list` over a large cluster becomes an
argument of its own size, then an argument of an argument."""


def refs_in(value: Any) -> set[str]:
    """Every ``${step}`` reachable in a string, list or mapping."""
    if isinstance(value, str):
        return set(REF.findall(value))
    if isinstance(value, dict):
        return set().union(*(refs_in(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(refs_in(item) for item in value)) if value else set()
    return set()


def substitute(value: Any, outputs: dict[str, str]) -> Any:
    """Replace every ``${step}`` with that step's output, in place of nothing.

    A reference with no output --- a step that was skipped, or one that
    produced nothing --- becomes the empty string rather than being left as
    the literal ``${step}``. Leaving it would send the text ``${step}`` to a
    cloud API as if it were a resource name, and a silent empty value is the
    lesser of two bad answers. ``validate`` is what stops it arising.
    """
    if isinstance(value, str):
        return REF.sub(lambda m: outputs.get(m.group(1), "")[:MAX_SUBSTITUTED], value)
    if isinstance(value, dict):
        return {key: substitute(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, outputs) for item in value]
    return value


def is_input(name: str) -> bool:
    """An input reference, available everywhere, rather than a step's output."""
    return name.startswith("inputs.")


def ancestors(step_id: str, needs: dict[str, list[str]]) -> set[str]:
    """Every step that is guaranteed to have run before ``step_id``.

    Walked rather than taken from ``needs`` directly: depending on a step that
    depends on another means both are guaranteed, and a reference to the
    grandparent is legitimate.
    """
    seen: set[str] = set()
    queue = list(needs.get(step_id, ()))
    while queue:
        current = queue.pop()
        if current in seen or current == step_id:
            continue
        seen.add(current)
        queue.extend(needs.get(current, ()))
    return seen
