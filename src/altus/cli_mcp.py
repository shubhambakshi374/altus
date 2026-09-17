"""``altus mcp`` --- Altus as an MCP server, and what it would offer.

Two commands, and the order matters. ``expose`` prints the surface without
starting anything, because a server whose surface you have to run it to
discover is one nobody audits. ``serve`` starts it.
"""

from __future__ import annotations

from typing import Any

import typer

app = typer.Typer(help="Offer Altus's own tools to an MCP client.")


@app.command("expose")
def mcp_expose(
    writes: bool = typer.Option(
        False, "--writes", help="Show the surface as it would be with allow_writes on."
    ),
) -> None:
    """Exactly what this machine would put on the wire, and what it would not."""
    from altus.config import load_config
    from altus.mcp.expose import annotations_for, exposed, withheld
    from altus.tools.base import sensitivity_of
    from altus.tools.registry import default_registry

    config = load_config()
    settings = config.mcp.expose.model_copy(
        update={"allow_writes": writes or None} if writes else {}
    )
    registry = default_registry(
        cloud=config.cloud, mcp_settings=config.mcp, shell=config.tools.shell
    )

    offered = exposed(registry, settings)
    state = "on" if settings.enabled else "off ([mcp.expose] enabled = false)"
    typer.echo(f"altus mcp serve: {state}")
    typer.echo(f"writes: {'offered' if settings.allow_writes else 'withheld'}\n")
    typer.echo(f"{len(offered)} tools would be offered:")
    for tool in offered:
        hints = annotations_for(tool)
        marks = "read-only" if hints["readOnlyHint"] else "destructive"
        world = " open-world" if hints["openWorldHint"] else ""
        typer.echo(f"  {tool.name:<22} {sensitivity_of(tool).value:<12} {marks}{world}")

    held = withheld(registry, settings)
    typer.echo(f"\n{len(held)} would not:")
    for name, why in held:
        typer.secho(f"  {name:<22} {why}", fg=typer.colors.BRIGHT_BLACK)

    if settings.workflows:
        _workflow_lines(config, registry)


def _workflow_lines(config: Any, registry: Any) -> None:
    from altus.core.errors import AltusError
    from altus.workflow import blast_radius, list_workflows, load

    names = list_workflows(config.workflow)
    if not names:
        return
    typer.echo("\nworkflows, each offered as a tool of its own:")
    for name in names:
        try:
            workflow = load(name, config.workflow)
        except AltusError as exc:
            typer.secho(f"  workflow_{name:<20} unreadable: {exc}", fg=typer.colors.YELLOW)
            continue
        radius = blast_radius(workflow, registry)
        held = (
            ""
            if config.mcp.expose.allow_writes or radius.level.value == "read"
            else "  (withheld: it changes things and allow_writes is false)"
        )
        typer.echo(f"  workflow_{name:<20} {len(workflow.steps)} steps  {radius.render()}{held}")
