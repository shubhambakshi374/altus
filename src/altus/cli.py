"""Command line entry point.

``altus`` with no subcommand launches the TUI. Everything else is deliberately
usable headlessly, so the provider layer can be exercised without a terminal
UI — and so CI can too.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import typer

from altus import __version__
from altus.agent import build_tool_context, build_workspace, run_agent
from altus.config import (
    config_path,
    load_config,
    resolve_profile,
    save_config,
    sessions_dir,
    write_starter_config,
)
from altus.config.models import Profile
from altus.config.secrets import (
    PROVIDER_NAMES_HINT,
    credential_status,
    delete_api_key,
    set_api_key,
)
from altus.core.errors import AltusError
from altus.core.events import StreamError, TextDelta, ToolDenied, ToolFinished, ToolStarted
from altus.core.session import Session
from altus.core.types import Message, ModelInfo
from altus.providers import PROVIDER_NAMES, create_provider
from altus.storage.sessions import SessionStore
from altus.tools import default_registry
from altus.tools.approval import AllowAll, DenyAll, SessionApprovals

app = typer.Typer(
    name="altus",
    help="A TUI coding and DevOps harness with BYOK multi-provider LLM support.",
    no_args_is_help=False,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect configuration and manage API keys.")
providers_app = typer.Typer(help="Inspect providers and their models.")
sessions_app = typer.Typer(help="Browse saved sessions.")
tools_app = typer.Typer(help="Inspect the tools the model can call.")
cloud_app = typer.Typer(help="Cloud authentication and status.")
kube_app = typer.Typer(help="Kubernetes contexts.")
app.add_typer(config_app, name="config")
app.add_typer(providers_app, name="providers")
app.add_typer(sessions_app, name="sessions")
app.add_typer(tools_app, name="tools")
app.add_typer(cloud_app, name="cloud")
app.add_typer(kube_app, name="kube")


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if version:
        typer.echo(f"altus {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _launch_tui(profile=None, resume=None)


@app.command()
def chat(
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    resume: Annotated[str | None, typer.Option("--resume", help="Session id, or 'last'.")] = None,
    once: Annotated[
        str | None,
        typer.Option("--once", help="Send one prompt, stream to stdout, exit. No TUI."),
    ] = None,
    no_tools: Annotated[
        bool, typer.Option("--no-tools", help="Disable filesystem tools; plain chat.")
    ] = False,
    allow_path: Annotated[
        list[str] | None,
        typer.Option("--allow-path", help="Extra readable root. Repeatable."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Approve file changes without asking. --once cannot prompt, so "
            "writes are refused without this.",
        ),
    ] = False,
) -> None:
    """Start the chat TUI, or run a single headless turn with --once."""
    extra = tuple(allow_path or ())
    if once is None:
        _launch_tui(
            profile=profile,
            resume=resume,
            model=model,
            provider=provider,
            no_tools=no_tools,
            extra_roots=extra,
            auto_approve=yes,
        )
        return
    try:
        raise SystemExit(
            asyncio.run(_chat_once(once, profile, model, provider, no_tools, extra, yes))
        )
    except AltusError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


async def _chat_once(
    prompt: str,
    profile_name: str | None,
    model: str | None,
    provider_name: str | None,
    no_tools: bool,
    extra_roots: tuple[str, ...],
    auto_approve: bool,
) -> int:
    """One prompt through the full agent loop.

    Text goes to stdout and tool activity to stderr, so the answer stays
    pipeable while the tool trace stays visible.
    """
    config = load_config()
    _, prof = resolve_profile(config, profile_name)
    prof = _override(prof, model=model, provider=provider_name)

    workspace = build_workspace(config, extra_roots=extra_roots)
    registry = default_registry(cloud=config.cloud, mcp_settings=config.mcp)
    # --once is non-interactive by definition: there is nobody to prompt, so
    # file changes fail closed unless the caller passed --yes.
    ctx = build_tool_context(
        config, workspace, approvals=SessionApprovals(AllowAll() if auto_approve else DenyAll())
    )
    tools_on = config.tools.enabled and not no_tools

    session = Session(
        provider=prof.provider,
        model=prof.model,
        system=prof.system,
        max_tokens=prof.max_tokens,
        temperature=prof.temperature,
        base_url=prof.base_url,
        model_supports_tools=prof.supports_tools is not False,
        workspace_root=str(workspace.root),
        tools_enabled=tools_on,
    )
    session.append(Message.user(prompt))

    adapter = create_provider(prof.provider, config, profile=prof)
    failed = False
    mid_line = False
    try:
        async for event in run_agent(
            adapter, session, registry, ctx, max_iterations=config.tools.max_iterations
        ):
            if isinstance(event, TextDelta):
                sys.stdout.write(event.text)
                sys.stdout.flush()
                mid_line = not event.text.endswith("\n")
            elif isinstance(event, ToolStarted):
                if mid_line:
                    # stdout and stderr share a terminal; don't splice the tool
                    # trace into the middle of a sentence.
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    mid_line = False
                typer.secho(
                    f"  → {event.name}({_brief(event.args)})",
                    fg=typer.colors.BRIGHT_BLACK,
                    err=True,
                )
            elif isinstance(event, ToolDenied):
                typer.secho(
                    f"    refused: {event.name} (re-run with --yes to allow changes)",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
            elif isinstance(event, ToolFinished):
                colour = typer.colors.RED if event.is_error else typer.colors.BRIGHT_BLACK
                typer.secho(f"    {event.summary}", fg=colour, err=True)
                failed = failed or event.is_error
            elif isinstance(event, StreamError):
                typer.secho(f"\nstream error: {event.message}", fg=typer.colors.RED, err=True)
                failed = True
    finally:
        await adapter.close()

    if mid_line:
        sys.stdout.write("\n")
    typer.secho(
        f"[{prof.provider}/{prof.model}] "
        f"{session.usage.input_tokens} in / {session.usage.output_tokens} out"
        + (f" · workspace {workspace.root}" if tools_on else " · tools off")
        + (" · writes approved" if tools_on and auto_approve else ""),
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )
    return 1 if failed else 0


def _brief(args: dict[str, object], limit: int = 60) -> str:
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _override(profile: Profile, *, model: str | None, provider: str | None) -> Profile:
    updates: dict[str, str] = {}
    if model:
        updates["model"] = model
    if provider:
        if provider not in PROVIDER_NAMES:
            _fail(f"unknown provider {provider!r} (known: {PROVIDER_NAMES_HINT})")
        updates["provider"] = provider
    return profile.model_copy(update=updates) if updates else profile


def _launch_tui(
    *,
    profile: str | None,
    resume: str | None,
    model: str | None = None,
    provider: str | None = None,
    no_tools: bool = False,
    extra_roots: tuple[str, ...] = (),
    auto_approve: bool = False,
) -> None:
    from altus.tui.app import AltusApp

    try:
        AltusApp(
            profile_name=profile,
            resume=resume,
            model_override=model,
            provider_override=provider,
            no_tools=no_tools,
            extra_roots=extra_roots,
            auto_approve=auto_approve,
        ).run()
    except AltusError as exc:
        _fail(str(exc))


# --------------------------------------------------------------------------- config


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    typer.echo(str(config_path()))


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration. Contains no secrets."""
    import tomli_w

    config = load_config()
    typer.echo(tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)).rstrip())


@config_app.command("init")
def config_init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter config file."""
    path = config_path()
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite)")
    written = write_starter_config()
    typer.echo(f"wrote {written}")


@config_app.command("set-key")
def config_set_key(
    provider: Annotated[str, typer.Argument(help=f"One of: {', '.join(PROVIDER_NAMES)}")],
) -> None:
    """Store an API key in the OS keyring. Never written to disk in plaintext."""
    if provider not in PROVIDER_NAMES:
        _fail(f"unknown provider {provider!r} (known: {PROVIDER_NAMES_HINT})")
    from altus.config.secrets import USES_CREDENTIAL_CHAIN

    if provider in USES_CREDENTIAL_CHAIN:
        _fail(
            f"{provider} authenticates through its cloud SDK credential chain "
            "(AWS_PROFILE, instance/IRSA roles), not an API key"
        )
    key = typer.prompt(f"{provider} API key", hide_input=True)
    if not key.strip():
        _fail("empty key")
    try:
        set_api_key(provider, key.strip())
    except Exception as exc:
        _fail(f"keyring unavailable: {exc}")
    typer.secho(f"stored key for {provider} in the OS keyring", fg=typer.colors.GREEN)


@config_app.command("delete-key")
def config_delete_key(provider: str) -> None:
    """Remove a stored API key from the OS keyring."""
    typer.echo(
        f"deleted key for {provider}"
        if delete_api_key(provider)
        else f"no stored key for {provider}"
    )


@config_app.command("doctor")
def config_doctor() -> None:
    """Report which providers can authenticate. Never prints key material."""
    config = load_config()
    typer.echo(f"config:   {config_path()}")
    typer.echo(f"sessions: {sessions_dir()}")
    typer.echo(f"default profile: {config.default_profile}\n")
    ok = 0
    for name in PROVIDER_NAMES:
        status = credential_status(name)
        if status.available:
            ok += 1
        typer.secho(
            f"  {'ok  ' if status.available else 'none'}  {name:<14} "
            f"{status.source:<10} {status.detail}",
            fg=typer.colors.GREEN if status.available else typer.colors.YELLOW,
        )
    typer.echo(f"\n{ok}/{len(PROVIDER_NAMES)} providers configured")


# ------------------------------------------------------------------------ providers


@providers_app.command("list")
def providers_list() -> None:
    """List supported providers and their credential status."""
    for name in PROVIDER_NAMES:
        status = credential_status(name)
        typer.echo(f"  {name:<14} {'configured' if status.available else '-'}")


@providers_app.command("models")
def providers_models(
    provider: str,
    live: Annotated[
        bool, typer.Option("--live", help="Query the provider instead of the local catalog.")
    ] = False,
) -> None:
    """List a provider's models."""
    if provider not in PROVIDER_NAMES:
        _fail(f"unknown provider {provider!r} (known: {PROVIDER_NAMES_HINT})")
    from altus.providers.registry import catalog_for

    if not live:
        models = catalog_for(provider)
    else:
        config = load_config()
        _, prof = resolve_profile(config, None)
        adapter = create_provider(
            provider, config, profile=prof if prof.provider == provider else None
        )

        async def _go() -> list[ModelInfo]:
            try:
                return await adapter.list_models()
            finally:
                await adapter.close()

        try:
            models = asyncio.run(_go())
        except AltusError as exc:
            _fail(str(exc))
    if not models:
        typer.echo("no models known; try --live")
        return
    for model in models:
        typer.echo(f"  {model.id:<50} {model.label}")


# ------------------------------------------------------------------------- sessions


@sessions_app.command("list")
def sessions_list(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """List saved sessions, newest first."""
    store = SessionStore(sessions_dir())
    found = store.list_sessions(limit=limit)
    if not found:
        typer.echo("no sessions yet")
        return
    for session in found:
        stamp = session.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        typer.echo(f"  {session.id}  {stamp}  {session.model:<28} {session.title or '-'}")


@sessions_app.command("show")
def sessions_show(session_id: str) -> None:
    """Print a session transcript."""
    store = SessionStore(sessions_dir())
    if session_id == "last":
        latest = store.latest_id()
        if latest is None:
            _fail("no sessions yet")
        session_id = latest or ""
    try:
        session = store.load(session_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    for message in session.messages:
        typer.secho(f"\n{message.role.value}:", fg=typer.colors.CYAN, bold=True)
        typer.echo(message.text)


@sessions_app.command("rm")
def sessions_rm(session_id: str) -> None:
    """Delete a saved session."""
    store = SessionStore(sessions_dir())
    typer.echo(
        f"deleted {session_id}" if store.delete(session_id) else f"no such session: {session_id}"
    )


@tools_app.command("list")
def tools_list() -> None:
    """List the tools the model can call, and the workspace they operate in."""
    config = load_config()
    workspace = build_workspace(config)
    registry = default_registry(cloud=config.cloud, mcp_settings=config.mcp)
    state = "enabled" if config.tools.enabled else "disabled"
    typer.echo(f"workspace: {workspace.root}")
    for extra in workspace.extra_roots:
        typer.echo(f"  + {extra}")
    typer.echo(f"tools:     {state} (max {config.tools.max_iterations} iterations/turn)\n")
    for tool in sorted(registry, key=lambda t: t.name):
        access = "read-only" if tool.read_only else "needs approval"
        typer.echo(f"  {tool.name:<12} [{access:^14}]  {tool.description.split('.')[0]}.")
    # An uninstalled integration must be a visible, fixable state --- not a
    # tool that silently is not there. The /tools slash command says the same.
    from altus.cloud.base import missing_integrations

    if missing := missing_integrations():
        typer.echo("\nnot installed:")
        for entry in missing:
            typer.secho(
                f"  {entry.name:<6} {entry.summary:<26} {entry.install_hint}",
                fg=typer.colors.BRIGHT_BLACK,
            )
    if config.workspace.deny_secrets:
        typer.echo(
            "\ncredential-shaped files (.env, private keys) are blocked inside the workspace"
        )


# --------------------------------------------------------------------- cloud


@app.command("login")
def login_cmd(
    cloud: Annotated[str | None, typer.Argument(help="k8s, aws, azure or gcp.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="AWS profile.")] = None,
) -> None:
    """Show cloud auth status, or sign in to one."""
    from altus.cloud.auth import all_status, login, status

    config = load_config()
    extra = tuple(config.cloud.kubeconfigs)
    if cloud is None:
        for entry in all_status(extra):
            colour = typer.colors.GREEN if entry.authenticated else typer.colors.YELLOW
            typer.secho(
                f"  {entry.cloud:<6} {entry.state:<16} {entry.source:<18} "
                f"{entry.detail or entry.hint}",
                fg=colour if entry.available else typer.colors.BRIGHT_BLACK,
            )
        return

    current = status(cloud, extra_kubeconfigs=extra)
    if not current.available:
        _fail(f"{cloud} support is not installed: {current.hint}")
    ok, message = asyncio.run(login(cloud, profile=profile))
    typer.secho(message, fg=typer.colors.GREEN if ok else typer.colors.RED)
    raise typer.Exit(0 if ok else 1)


@cloud_app.command("status")
def cloud_status() -> None:
    """Credential status for every cloud. Local only --- no network calls."""
    login_cmd(cloud=None, profile=None)


@kube_app.command("list")
def kube_list() -> None:
    """List Kubernetes contexts and mark protected ones."""
    from altus.cloud.base import ProtectionRules
    from altus.cloud.kube import list_contexts

    config = load_config()
    contexts, active = list_contexts(tuple(config.cloud.kubeconfigs))
    if not contexts:
        typer.echo("no kubeconfig found; set KUBECONFIG or run: altus kube add <path>")
        return
    rules = ProtectionRules.build(
        config.cloud.protected.patterns,
        config.cloud.protected.accounts,
        config.cloud.protected.mode,
    )
    selected = config.cloud.kube_context or active
    for context in contexts:
        mark = "→" if context.name == selected else " "
        protected = " ⚠ protected" if rules.matches(context.target()) else ""
        typer.echo(f" {mark} {context.name:<46} ns={context.namespace}{protected}")


@kube_app.command("use")
def kube_use(
    context: str,
    global_scope: Annotated[
        bool, typer.Option("--global", help="Also set current-context in your kubeconfig.")
    ] = False,
    local_scope: Annotated[
        bool, typer.Option("--local", help="Keep the selection to Altus only.")
    ] = False,
) -> None:
    """Select a context. By default only Altus follows it; --global writes the kubeconfig."""
    from altus.cloud.kube import list_contexts, set_current_context

    config = load_config()
    extra = tuple(config.cloud.kubeconfigs)
    contexts, _ = list_contexts(extra)
    if not any(c.name == context for c in contexts):
        _fail(f"no context named {context!r}")

    scope = config.cloud.kube_context_scope
    if global_scope:
        scope = "global"
    elif local_scope:
        scope = "altus"

    config.cloud.kube_context = context
    save_config(config)
    typer.secho(f"using {context}", fg=typer.colors.GREEN)
    if scope == "global":
        try:
            path = set_current_context(context, extra)
        except Exception as exc:
            _fail(f"could not update the kubeconfig: {exc}")
        else:
            typer.secho(
                f"also set current-context in {path} — other terminals will follow",
                fg=typer.colors.YELLOW,
            )


@kube_app.command("add")
def kube_add(path: str) -> None:
    """Register an extra kubeconfig file."""
    from pathlib import Path

    resolved = Path(path).expanduser()
    if not resolved.is_file():
        _fail(f"no such file: {resolved}")
    config = load_config()
    if str(resolved) not in config.cloud.kubeconfigs:
        config.cloud.kubeconfigs.append(str(resolved))
        save_config(config)
    typer.secho(f"registered {resolved}", fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
