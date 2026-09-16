"""The built-in slash commands.

These are a new front door onto machinery that already exists ---
``config.secrets``, ``providers.registry``, ``cloud.auth``, ``cloud.kube`` and
the existing ``ModelPicker`` --- rather than new machinery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from altus.tui.commands.registry import Command, CommandRegistry, CommandResult

if TYPE_CHECKING:
    from altus.tui.app import AltusApp

CLOUDS = ("k8s", "aws", "azure", "gcp")


# ------------------------------------------------------------------------ help


async def cmd_help(app: AltusApp, args: list[str]) -> CommandResult:
    lines = ["Commands:"]
    for command in app.commands.unique:
        lines.append(f"  /{command.usage or command.name:<26} {command.summary}")
    return CommandResult("\n".join(lines), title="Help")


# ------------------------------------------------------------------- providers


async def cmd_provider(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.config.secrets import credential_status
    from altus.providers import PROVIDER_NAMES
    from altus.tui.widgets.provider_picker import ProviderPicker

    if not args:
        # A list you cannot act on is a dead end. Choosing a configured
        # provider switches; choosing an unconfigured one opens its setup.
        chosen = await app.push_screen_wait(ProviderPicker())
        if not chosen:
            return CommandResult.silent()
        if not credential_status(chosen).available:
            app.open_setup(chosen)
            return CommandResult.silent()
        await app.switch_provider(chosen)
        return CommandResult(f"Switched to {chosen} ({app.session.model}).")

    if args and args[0] == "use":
        if len(args) < 2:
            return CommandResult.error("usage: /provider use <name>")
        name = args[1]
        if name not in PROVIDER_NAMES:
            return CommandResult.error(
                f"Unknown provider {name!r}. Known: {', '.join(PROVIDER_NAMES)}"
            )
        if not credential_status(name).available:
            return CommandResult.error(f"No credentials for {name}. Run /key {name} first.")
        await app.switch_provider(name)
        return CommandResult(f"Switched to {name} ({app.session.model}).")

    return CommandResult.error(f"usage: /provider  or  /provider use <name>. Got: {args}")


async def cmd_key(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.config.secrets import USES_CREDENTIAL_CHAIN, delete_api_key
    from altus.providers import PROVIDER_NAMES
    from altus.tui.widgets.key_prompt import KeyPrompt

    if not args:
        return CommandResult.error("usage: /key <provider>   or   /key rm <provider>")

    if args[0] == "rm":
        if len(args) < 2:
            return CommandResult.error("usage: /key rm <provider>")
        removed = delete_api_key(args[1])
        return CommandResult(
            f"Removed the stored key for {args[1]}." if removed else f"No stored key for {args[1]}."
        )

    name = args[0]
    if name not in PROVIDER_NAMES:
        return CommandResult.error(f"Unknown provider {name!r}. Known: {', '.join(PROVIDER_NAMES)}")
    if name in USES_CREDENTIAL_CHAIN:
        return CommandResult.warn(
            f"{name} authenticates through its cloud credential chain, not an API key."
        )
    app.push_screen(KeyPrompt(name))
    return CommandResult.silent()


async def cmd_profile(app: AltusApp, args: list[str]) -> CommandResult:
    """Profiles are how a self-hosted endpoint is named, so they need a door."""
    profiles = app.config.profiles
    if args and args[0] == "use":
        if len(args) < 2:
            return CommandResult.error("usage: /profile use <name>")
        try:
            await app.use_profile(args[1])
        except KeyError:
            return CommandResult.error(
                f"No profile named {args[1]!r}. Known: {', '.join(sorted(profiles))}"
            )
        session = app.session
        where = f" at {session.base_url}" if session.base_url else ""
        return CommandResult(f"Using profile {args[1]}: {session.provider}/{session.model}{where}.")

    rows = ["Profiles:"]
    for name, profile in sorted(profiles.items()):
        mark = "→" if name == app.config.default_profile else " "
        where = f"  {profile.base_url}" if profile.base_url else ""
        rows.append(f" {mark} {name:<16} {profile.provider}/{profile.model}{where}")
    rows.append("\n  /profile use <name> to switch")
    return CommandResult("\n".join(rows), title="Profiles")


async def cmd_setup(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.providers import PROVIDER_NAMES

    provider = args[0] if args else None
    if provider and provider not in PROVIDER_NAMES:
        return CommandResult.error(
            f"Unknown provider {provider!r}. Known: {', '.join(PROVIDER_NAMES)}"
        )
    app.open_setup(provider)
    return CommandResult.silent()


async def cmd_model(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.core.types import ModelInfo
    from altus.providers.registry import known_models
    from altus.tui.widgets.model_picker import ModelPicker

    if not args:
        chosen = await app.push_screen_wait(ModelPicker())
        if chosen is None:
            return CommandResult.silent()
        await app.switch_model(chosen)
        return CommandResult(f"Model set to {chosen.id} ({chosen.provider}).")

    wanted = args[0]
    match = next((m for m in known_models() if m.id == wanted), None)
    if match is None:
        # Not in the catalog: accept it against the current provider anyway,
        # since catalogs go stale faster than providers ship models.
        match = ModelInfo(id=wanted, provider=app.session.provider)
    await app.switch_model(match)
    return CommandResult(f"Model set to {match.id} ({match.provider}).")


# ----------------------------------------------------------------------- cloud


async def cmd_login(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.cloud.auth import all_status, login, status

    extra = tuple(app.config.cloud.kubeconfigs)
    if not args:
        rows = ["Cloud authentication:"]
        for entry in await asyncio.to_thread(all_status, extra):
            mark = "●" if entry.authenticated else ("○" if entry.available else "·")
            rows.append(f"  {mark} {entry.cloud:<6} {entry.state:<16} {entry.detail or entry.hint}")
        rows.append("\n  /login <cloud> to sign in")
        return CommandResult("\n".join(rows), title="Cloud auth")

    cloud = args[0].casefold()
    if cloud not in CLOUDS:
        return CommandResult.error(f"Unknown cloud {cloud!r}. Known: {', '.join(CLOUDS)}")
    current = await asyncio.to_thread(status, cloud, extra_kubeconfigs=extra)
    if not current.available:
        return CommandResult.error(f"{cloud} support is not installed: {current.hint}")

    profile = args[args.index("--profile") + 1] if "--profile" in args else None
    ok, message = await login(cloud, profile=profile)
    return CommandResult(message, severity="information" if ok else "error", title=f"{cloud} login")


async def cmd_kube(app: AltusApp, args: list[str]) -> CommandResult:
    from altus.cloud.base import CloudTarget, ProtectionRules
    from altus.cloud.kube import list_contexts

    settings = app.config.cloud
    extra = tuple(settings.kubeconfigs)
    rules = ProtectionRules.build(
        settings.protected.patterns, settings.protected.accounts, settings.protected.mode
    )

    if args and args[0] == "use":
        if len(args) < 2:
            return CommandResult.error("usage: /kube use <context>")
        wanted = args[1]
        contexts, _ = await asyncio.to_thread(list_contexts, extra)
        if not any(c.name == wanted for c in contexts):
            return CommandResult.error(f"No context named {wanted!r}. Run /kube to list them.")

        scope = settings.kube_context_scope
        if "--global" in args:
            scope = "global"
        elif "--local" in args:
            scope = "altus"

        app.set_kube_context(wanted)
        written = ""
        if scope == "global":
            from altus.cloud.kube import set_current_context

            try:
                path = await asyncio.to_thread(set_current_context, wanted, extra)
            except Exception as exc:
                return CommandResult.warn(
                    f"Using context {wanted} in Altus, but the kubeconfig could not be updated: {exc}"
                )
            written = f"  Also set current-context in {path} — other terminals will follow."

        protected = rules.matches(CloudTarget("k8s", wanted))
        note = "  ⚠ protected: changes here need extra confirmation" if protected else ""
        return CommandResult(f"Using context {wanted}.{note}{written}")

    if args and args[0] == "add":
        if len(args) < 2:
            return CommandResult.error("usage: /kube add <path-to-kubeconfig>")
        resolved = await asyncio.to_thread(_resolve_file, args[1])
        if resolved is None:
            return CommandResult.error(f"No such file: {args[1]}")
        app.add_kubeconfig(resolved)
        return CommandResult(f"Registered {resolved}.")

    contexts, active = await asyncio.to_thread(list_contexts, extra)
    if not contexts:
        return CommandResult.warn(
            "No kubeconfig found. Add one with /kube add <path>, or set KUBECONFIG."
        )
    selected = settings.kube_context or active
    rows = ["Kubernetes contexts:"]
    for context in contexts:
        mark = "→" if context.name == selected else " "
        flag = " ⚠ protected" if rules.matches(context.target()) else ""
        rows.append(f" {mark} {context.name:<46} ns={context.namespace}{flag}")
    rows.append("\n  /kube use <context> to switch (your ~/.kube/config is never modified)")
    return CommandResult("\n".join(rows), title="Kubernetes")


def _resolve_file(raw: str) -> str | None:
    """Expand and stat off the event loop; None when it is not a file."""
    path = Path(raw).expanduser()
    return str(path) if path.is_file() else None


# ----------------------------------------------------------------------- misc


async def cmd_tools(app: AltusApp, args: list[str]) -> CommandResult:
    """What is registered, and what each one costs to call.

    The access column comes from ``sensitivity_of`` rather than being derived
    here, because ``/workflow`` asks the same question and two doors deciding
    separately is how one question gets two answers. It also fixes what the
    local version got wrong: it ran every tool's verb through the *Kubernetes*
    classifier, and Azure's ``write`` and ``action`` are verbs Kubernetes has
    never heard of, so they hit the unknown-verb fallback and every Azure
    mutation was reported as privileged.
    """
    from altus.cloud.base import INTEGRATIONS
    from altus.tools.base import dispatches, sensitivity_of

    rows = [f"Workspace: {app.workspace.root}", "", "Tools:"]
    varies = False
    for tool in sorted(app.registry, key=lambda t: t.name):
        level = sensitivity_of(tool)
        if not level.needs_approval:
            access = "read-only"
        elif level.needs_challenge:
            access = "type to confirm"
        else:
            access = "needs approval"
        mark = " *" if dispatches(tool) else ""
        varies = varies or bool(mark)
        rows.append(f"  {tool.name:<18} [{access:^15}]{mark}")
    if varies:
        rows.append(
            "\n  * one tool over a whole surface — the arguments decide, so the "
            "column above is a floor and the real level is settled at the gate"
        )

    from altus.tools.k8s import disabled_classes

    off = disabled_classes(app.config.cloud.k8s)
    if off:
        rows.append("\nSwitched off in [cloud.k8s] — not offered to the model:")
        rows += [f"  {text}" for text in off]
    missing = [i for i in INTEGRATIONS if not i.available]
    if missing:
        rows.append("\nNot installed:")
        rows += [f"  {i.name:<6} {i.summary:<24} {i.install_hint}" for i in missing]
    granted = app.approvals.always_allowed
    if granted:
        rows.append(f"\n⚠ standing approval this session: {', '.join(sorted(granted))}")
    return CommandResult("\n".join(rows), title="Tools")


async def cmd_aws(app: AltusApp, args: list[str]) -> CommandResult:
    """Identity, account and region --- and switching either.

    Switching asks nothing here because it changes no cloud state, but it does
    change the blast radius of every later call, so it reports what it moved to
    and whether that account is protected.
    """
    from altus.cloud.auth import status
    from altus.cloud.base import ProtectionRules
    from altus.config import save_config

    settings = app.config.cloud
    rules = ProtectionRules.build(
        settings.protected.patterns, settings.protected.accounts, settings.protected.mode
    )

    if args and args[0] == "region":
        if len(args) < 2:
            return CommandResult.error("usage: /aws region <name>")
        settings.default_region = args[1]
        save_config(app.config)
        provider = getattr(app.tool_ctx.cloud, "aws", None)
        if provider is not None:
            provider.region = args[1]
            provider.reset()
        app.tool_ctx.cloud.aws_region = args[1]
        return CommandResult(f"AWS region set to {args[1]}.")

    if args and args[0] == "profile":
        if len(args) < 2:
            return CommandResult.error("usage: /aws profile <name>")
        provider = getattr(app.tool_ctx.cloud, "aws", None)
        if provider is None:
            return CommandResult.error("no AWS session in this context")
        provider.profile = args[1]
        provider.reset()
        return CommandResult(
            f"Using AWS profile {args[1]} for this session. Set AWS_PROFILE to make it the default."
        )

    current = await asyncio.to_thread(status, "aws")
    if not current.authenticated:
        return CommandResult.warn(f"Not signed in to AWS: {current.detail or current.hint}")

    rows = ["AWS:", f"  {current.source or 'credentials'}  {current.detail}"]
    provider = getattr(app.tool_ctx.cloud, "aws", None)
    if provider is not None:
        try:
            identity = await provider.whoami()
        except Exception as exc:
            rows.append(f"  could not read identity: {exc}")
        else:
            from altus.cloud.aws import target_for

            region = settings.default_region or provider.default_region()
            protected = rules.matches(target_for(identity["account"], region))
            rows.append(f"  account  {identity['account']}")
            rows.append(f"  arn      {identity['arn']}")
            rows.append(f"  region   {region}{'  ⚠ protected' if protected else ''}")
    rows.append("\n  /aws region <name> · /aws profile <name>")
    return CommandResult("\n".join(rows), title="AWS")


async def cmd_dashboard(app: AltusApp, args: list[str]) -> CommandResult:
    """Four read-only views of one namespace, on one screen."""
    from altus.tui.screens.dashboard import DashboardScreen

    clouds = {"k8s", "aws", "azure", "gcp"}
    wanted = (args[0] if args else "").casefold()
    cloud = wanted if wanted in clouds else "k8s"
    rest = args[1:] if wanted in clouds else args

    hints = {
        "aws": "AWS tools are not available in this session.",
        "azure": "Azure tools are not available. Install with: uv sync --extra azure",
        "gcp": "GCP tools are not available. Install with: uv sync --extra gcp",
        "k8s": "Kubernetes tools are not available. Install with: uv sync --extra k8s",
    }
    if f"{cloud}_topology" not in app.registry:
        return CommandResult.error(hints[cloud])

    scope = rest[0] if rest else ("default" if cloud == "k8s" else "")
    app.push_screen(DashboardScreen(scope, setting=app.config.ui.graphics, cloud=cloud))
    return CommandResult.silent()


async def cmd_azure(app: AltusApp, args: list[str]) -> CommandResult:
    """Tenant, subscription and principal --- and switching subscription.

    Switching asks nothing here because it changes no cloud state, but it does
    change the blast radius of every later call, so it reports what it moved to
    and whether that subscription is protected.
    """
    from altus.cloud.auth import status
    from altus.cloud.azure import target_for
    from altus.cloud.base import ProtectionRules
    from altus.config import save_config

    settings = app.config.cloud
    rules = ProtectionRules.build(
        settings.protected.patterns, settings.protected.accounts, settings.protected.mode
    )
    provider = getattr(app.tool_ctx.cloud, "azure", None)

    if args and args[0] in {"sub", "subscription"}:
        if len(args) < 2:
            return CommandResult.error("usage: /azure sub <subscription-id>")
        chosen = args[1]
        settings.azure_subscription = chosen
        save_config(app.config)
        if provider is not None:
            provider.subscription = chosen
            provider.reset()
        app.tool_ctx.cloud.azure_subscription = chosen
        protected = rules.matches(target_for(chosen))
        note = "  ⚠ this subscription is protected" if protected else ""
        return CommandResult(f"Azure subscription set to {chosen}.{note}")

    current = await asyncio.to_thread(status, "azure")
    if not current.authenticated:
        return CommandResult.warn(f"Not signed in to Azure: {current.detail or current.hint}")

    rows = ["Azure:", f"  {current.source or 'credentials'}  {current.detail}"]
    if provider is not None:
        try:
            identity = await provider.whoami()
        except Exception as exc:
            rows.append(f"  could not read identity: {exc}")
        else:
            subscription = settings.azure_subscription or ""
            protected = bool(subscription) and rules.matches(target_for(subscription))
            rows.append(f"  tenant        {identity['tenant']}")
            rows.append(f"  principal     {identity['principal'] or identity['object_id']}")
            rows.append(
                f"  subscription  {subscription or '(none selected)'}"
                f"{'  ⚠ protected' if protected else ''}"
            )
    rows.append("\n  /azure sub <id>   ·   list them with the azure_subscriptions tool")
    return CommandResult("\n".join(rows), title="Azure")


async def cmd_mcp(app: AltusApp, args: list[str]) -> CommandResult:
    """What is connected, what it covers, and what is missing.

    `/mcp check` connects to each enabled server and compares its live tool
    list against Altus's manifest. That is the only way drift becomes visible,
    and drift is the thing this surface has instead of a corpus: a tool name
    Altus has never seen classifies privileged, which is safe but is also the
    signal that a vendor shipped a release.
    """
    from altus.mcp.catalog import CATALOG, enabled_servers, why_off
    from altus.mcp.classify import drift, manifest

    settings = app.config.mcp
    if not settings.enabled:
        return CommandResult.warn("MCP is disabled ([mcp] enabled = false).")
    provider = getattr(app.tool_ctx.cloud, "mcp", None)
    if provider is None:
        return CommandResult.warn("The MCP extra is not installed. uv sync --extra mcp")

    # The same predicate the mcp_servers tool uses, so one question cannot get
    # two answers depending on which door it came through.
    enabled = [spec.id for spec in enabled_servers(settings)]
    rows = ["MCP servers:"]
    for spec in CATALOG:
        state = "ready" if spec.id in enabled else f"off --- {why_off(spec, settings)}"
        table = manifest(spec.id)
        rows.append(f"  {spec.id:<11} {state}")
        rows.append(f"    {', '.join(spec.products)}")
        rows.append(f"    {len(table.tools)} tools in Altus's manifest ({table.source})")
        if spec.notes:
            rows.append(f"    note: {spec.notes}")

    if args and args[0] == "check":
        rows.append("")
        rows.append("Comparing live tool lists against the manifests:")
        for server in enabled:
            try:
                live = await provider.tools(server)
            except Exception as exc:
                rows.append(f"  {server}: could not connect --- {exc}")
                continue
            found = drift(server, list(live), scope=provider.scopes.get(server, ""))
            rows.append(f"  {server}: {len(live)} live, {len(found)} disagreeing")
            rows += [f"    {line}" for line in found[:10]]
            if len(found) > 10:
                rows.append(f"    ... and {len(found) - 10} more")
    elif enabled:
        rows.append("")
        rows.append("/mcp check compares each live tool list against the manifest.")

    rows.append("")
    rows.append(
        "Writes are " + ("on" if settings.allow_writes else "off") + "; MCP has no dry-run, "
        "so a write prompt can only show current state, never a preview."
    )
    return CommandResult("\n".join(rows))


async def cmd_gcp(app: AltusApp, args: list[str]) -> CommandResult:
    """Account, project and protected status --- and switching project.

    Switching asks nothing here because it changes no cloud state, but it does
    change the blast radius of every later call, so it reports what it moved to
    and whether that project is protected.
    """
    from altus.cloud.auth import status
    from altus.cloud.base import ProtectionRules
    from altus.cloud.gcp import target_for
    from altus.config import save_config

    settings = app.config.cloud
    rules = ProtectionRules.build(
        settings.protected.patterns, settings.protected.accounts, settings.protected.mode
    )
    provider = getattr(app.tool_ctx.cloud, "gcp", None)

    if args and args[0] in {"project", "proj"}:
        if len(args) < 2:
            return CommandResult.error("usage: /gcp project <project-id>")
        chosen = args[1]
        settings.gcp_project = chosen
        save_config(app.config)
        if provider is not None:
            provider.project = chosen
            provider.reset()
        app.tool_ctx.cloud.gcp_project = chosen
        note = "  ⚠ this project is protected" if rules.matches(target_for(chosen)) else ""
        return CommandResult(f"GCP project set to {chosen}.{note}")

    current = await asyncio.to_thread(status, "gcp")
    if not current.authenticated:
        return CommandResult.warn(f"Not signed in to GCP: {current.detail or current.hint}")

    rows = ["GCP:", f"  {current.source or 'credentials'}  {current.detail}"]
    if provider is not None:
        try:
            identity = await provider.whoami()
        except Exception as exc:
            rows.append(f"  could not read identity: {exc}")
        else:
            project = settings.gcp_project or identity.get("project", "")
            protected = bool(project) and rules.matches(target_for(project))
            rows.append(f"  account   {identity['account'] or '(not named by these credentials)'}")
            rows.append(
                f"  project   {project or '(none selected)'}{'  ⚠ protected' if protected else ''}"
            )
    rows.append("\n  /gcp project <id>   ·   list them with the gcp_projects tool")
    return CommandResult("\n".join(rows), title="GCP")


async def cmd_graphics(app: AltusApp, args: list[str]) -> CommandResult:
    """What is being drawn and why --- the answer to "where are my pictures"."""
    from altus.render.capability import Support, available, detect, explain, images_installed

    settings = app.config.ui
    if args:
        wanted = args[0].casefold()
        if wanted not in {"auto", "image", "cells", "off"}:
            return CommandResult.error("usage: /graphics [auto | image | cells | off]")
        from altus.config import save_config

        settings.graphics = wanted  # type: ignore[assignment]
        save_config(app.config)
        return CommandResult(f"Graphics set to {wanted}. {explain(wanted)}.")

    rows = [
        f"Setting:   {settings.graphics}",
        f"Terminal:  {detect().value}  ({explain(settings.graphics)})",
        f"Installed: {'yes' if images_installed() else 'no — uv sync --extra graphics'}",
        "",
        "  image  Kitty protocol or Sixel. Kitty, Ghostty, WezTerm, iTerm2.",
        "  cells  Box-drawing characters. Works everywhere, labels stay crisp.",
        "  off    The plain text views.",
        "",
        "  /graphics <mode> to change it",
    ]
    if available() is not Support.IMAGE and settings.graphics in {"auto", "image"}:
        rows.insert(3, "  Topology still draws as a map; only the charts lose detail.")
    return CommandResult("\n".join(rows), title="Graphics")


async def cmd_new(app: AltusApp, args: list[str]) -> CommandResult:
    await app.new_session_from_command()
    return CommandResult.silent()


def build_registry() -> CommandRegistry:
    registry = CommandRegistry()
    for command in (
        Command("help", "Show this list", "help", cmd_help, aliases=("?",)),
        Command(
            "setup",
            "Add a provider: key, health check, default model",
            "setup [<provider>]",
            cmd_setup,
        ),
        Command(
            "provider",
            "Switch provider (opens setup if it needs a key)",
            "provider [use <name>]",
            cmd_provider,
            aliases=("providers",),
        ),
        Command("key", "Store or remove an API key", "key <provider> | rm <provider>", cmd_key),
        Command(
            "model",
            "Pick a model — searches the provider for unlisted ids",
            "model [<id>]",
            cmd_model,
            aliases=("models",),
        ),
        Command(
            "profile",
            "List profiles, or switch to one",
            "profile [use <name>]",
            cmd_profile,
            aliases=("profiles",),
        ),
        Command("login", "Cloud auth status, or sign in", "login [<cloud>]", cmd_login),
        Command("kube", "Kubernetes contexts", "kube [use <ctx> | add <path>]", cmd_kube),
        Command(
            "aws",
            "AWS identity, account and region",
            "aws [region <name> | profile <name>]",
            cmd_aws,
        ),
        Command(
            "azure",
            "Azure tenant, subscription and identity",
            "azure [sub <id>]",
            cmd_azure,
        ),
        Command("gcp", "GCP account, project and identity", "gcp [project <id>]", cmd_gcp),
        Command("mcp", "MCP servers and their tools", "mcp [check]", cmd_mcp),
        Command("tools", "Tools and installed integrations", "tools", cmd_tools),
        Command(
            "dashboard",
            "Several read-only views on one screen",
            "dashboard [aws | azure | gcp | k8s] [<scope>]",
            cmd_dashboard,
        ),
        Command(
            "graphics",
            "How visuals are drawn, and why",
            "graphics [auto | image | cells | off]",
            cmd_graphics,
        ),
        Command("new", "Start a new session", "new", cmd_new, aliases=("clear",)),
    ):
        registry.register(command)
    return registry
