"""The agent loop: inference, tool execution, repeat.

Composes ``runner.stream_turn`` (one inference call) with tool execution. The
CLI, the TUI and the Phase 2 workflow engine all drive it, so it stays free of
any UI import.

Three invariants this module exists to hold:

* **Every ``tool_use`` gets a matching ``tool_result``** --- including when the
  turn is cancelled and when the iteration cap is hit. Anthropic and Bedrock
  reject a conversation containing an unanswered ``tool_use``, so skipping the
  results on Ctrl+C would leave a session that can never be resumed.
* **Reasoning blocks survive between iterations**, signature included, because
  the providers that verify them need them echoed back verbatim.
* **The loop terminates visibly.** Hitting the cap tells the model so, rather
  than stalling silently.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from altus.core.events import (
    AgentEvent,
    IterationEnd,
    ToolDenied,
    ToolFinished,
    ToolStarted,
)
from altus.core.session import Session
from altus.core.types import Message, Role, StopReason, ToolResultBlock, ToolUseBlock
from altus.providers.base import Provider
from altus.runner import TurnAccumulator, stream_turn
from altus.tools.approval import ApprovalPolicy, DenyAll
from altus.tools.base import CloudContext, ToolContext, ToolOutcome
from altus.tools.registry import ToolRegistry
from altus.workspace import Workspace

if TYPE_CHECKING:
    from altus.config.models import Config

DEFAULT_MAX_ITERATIONS = 25
DEFAULT_OUTPUT_BUDGET = 100 * 1024
"""Total tool-result bytes per iteration. One unbounded read can otherwise
exhaust the context window and end the session."""

CANCELLED_RESULT = "Cancelled by the user before this tool ran."
BUDGET_NOTE = "\n\n[truncated: this turn's tool output budget is exhausted]"


def build_system_prompt(session: Session, registry: ToolRegistry) -> str | None:
    """Compose the profile's system prompt with a workspace preamble."""
    parts: list[str] = []
    if session.system:
        parts.append(session.system)
    if session.tools_enabled and session.model_supports_tools and len(registry):
        # Describe the registry actually in hand. Saying "read-only" while
        # write_file is declared taught the model to refuse edits it could
        # have made, and to tell the user it had no access it did have.
        can_write = any(not tool.read_only for tool in registry)
        access = (
            "You can read and modify a workspace on the user's machine"
            if can_write
            else "You have read-only access to a workspace on the user's machine"
        )
        parts.append(
            f"{access}, rooted at {session.workspace_root}.\n"
            f"Tools available: {', '.join(registry.names)}.\n"
            + (
                "Every write or deletion asks the user for approval first, so "
                "propose the edit by making the call rather than printing a patch "
                "and asking permission in prose.\n"
                if can_write
                else ""
            )
            + "Paths are relative to the workspace root. You cannot read outside it, "
            "and credential files (.env, private keys, and similar) are blocked by "
            "design. Prefer glob and grep to locate code before reading whole files."
        )
    if session.tools_enabled and not session.model_supports_tools:
        parts.append(
            f"The selected model ({session.model}) does not support tool calling, "
            "so you have no filesystem or Kubernetes tools in this session. Answer "
            "from the conversation, and say when something would need a tool."
        )
    return "\n\n".join(parts) if parts else None


@dataclass
class _Batch:
    blocks: list[ToolResultBlock] = field(default_factory=list)
    finished: list[ToolFinished] = field(default_factory=list)
    denied: list[ToolDenied] = field(default_factory=list)


async def run_agent(
    provider: Provider,
    session: Session,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    output_budget: int = DEFAULT_OUTPUT_BUDGET,
    max_attempts: int = 4,
) -> AsyncIterator[AgentEvent]:
    """Drive a full turn to completion, executing tools as the model asks.

    Appends every message it produces to ``session`` and yields the wider
    ``AgentEvent`` union so callers can render tool activity.
    """
    # A model that cannot call tools produces hallucinated call syntax or a
    # hard error when offered them. Declaring none is the honest degradation.
    offering_tools = session.tools_enabled and session.model_supports_tools
    tool_defs = registry.to_tool_defs() if offering_tools else []
    system = build_system_prompt(session, registry)

    for iteration in range(max_iterations):
        request = session.to_request(tools=tool_defs)
        request.system = system
        acc = TurnAccumulator()

        try:
            async for event in stream_turn(provider, request, acc, max_attempts=max_attempts):
                yield event
        except asyncio.CancelledError:
            # Persist whatever streamed, and answer any tool_use it already
            # contains -- an unanswered one makes the session unresumable.
            _append_turn(session, acc, cancelled=True)
            raise

        _append_turn(session, acc, cancelled=False)
        pending = list(acc.tool_uses)

        if acc.stop_reason is not StopReason.TOOL_USE or not pending:
            yield IterationEnd(index=iteration, tool_calls=0, final=True)
            return

        for call in pending:
            yield ToolStarted(id=call.id, name=call.name, args=call.input)

        try:
            batch = await _execute_all(pending, registry, ctx, output_budget)
        except asyncio.CancelledError:
            session.append(_results_message(_cancelled_results(pending)))
            raise

        for denied in batch.denied:
            yield denied
        for finished in batch.finished:
            yield finished

        final = iteration == max_iterations - 1
        if final:
            batch.blocks[-1] = _with_cap_notice(batch.blocks[-1], max_iterations)

        session.append(_results_message(batch.blocks))
        yield IterationEnd(index=iteration, tool_calls=len(pending), final=final)
        if final:
            return


def _append_turn(session: Session, acc: TurnAccumulator, *, cancelled: bool) -> None:
    """Append the assistant message, plus stand-in results if we are unwinding."""
    message = acc.to_message()
    if not message.content:
        return
    session.append(message)
    session.record_usage(acc.usage)
    if cancelled and acc.tool_uses:
        session.append(_results_message(_cancelled_results(acc.tool_uses)))


async def _execute_all(
    calls: list[ToolUseBlock],
    registry: ToolRegistry,
    ctx: ToolContext,
    output_budget: int,
) -> _Batch:
    """Read-only calls run concurrently; mutating ones run one at a time.

    Two edits to the same file would otherwise race on read-modify-write, and
    two approval prompts would race to reach the screen. Order within the
    turn is preserved either way, so results still line up with the calls.
    """
    outcomes: dict[int, ToolOutcome] = {}
    timings: dict[int, int] = {}
    concurrent = [(i, c) for i, c in enumerate(calls) if registry.is_read_only(c.name)]
    sequential = [(i, c) for i, c in enumerate(calls) if not registry.is_read_only(c.name)]

    if concurrent:
        began = time.monotonic()
        results = await asyncio.gather(
            *(registry.execute(c.name, c.input, ctx) for _, c in concurrent)
        )
        elapsed = int((time.monotonic() - began) * 1000)
        for (index, _), outcome in zip(concurrent, results, strict=True):
            outcomes[index] = outcome
            timings[index] = elapsed

    for index, call in sequential:
        began = time.monotonic()
        outcomes[index] = await registry.execute(call.name, call.input, ctx)
        timings[index] = int((time.monotonic() - began) * 1000)

    batch = _Batch()
    remaining = output_budget
    for index, call in enumerate(calls):
        outcome = outcomes[index]
        content, remaining = _clip(outcome.content, remaining)
        batch.blocks.append(
            ToolResultBlock(tool_use_id=call.id, content=content, is_error=outcome.is_error)
        )
        if outcome.denied:
            batch.denied.append(ToolDenied(id=call.id, name=call.name, reason=outcome.content))
        batch.finished.append(
            ToolFinished(
                id=call.id,
                name=call.name,
                summary=outcome.summary,
                is_error=outcome.is_error,
                duration_ms=timings[index],
                visual=outcome.visual,
            )
        )
    return batch


def _clip(content: str, remaining: int) -> tuple[str, int]:
    if remaining <= 0:
        return BUDGET_NOTE.strip(), 0
    if len(content) <= remaining:
        return content, remaining - len(content)
    return content[:remaining] + BUDGET_NOTE, 0


def _with_cap_notice(block: ToolResultBlock, cap: int) -> ToolResultBlock:
    """Carry the stop notice on the last real result, never as a dangling block."""
    return ToolResultBlock(
        tool_use_id=block.tool_use_id,
        content=(
            f"{block.content}\n\n[reached the {cap}-iteration limit; "
            "summarize what you found and what still needs doing]"
        ),
        is_error=block.is_error,
    )


def _cancelled_results(calls: list[ToolUseBlock]) -> list[ToolResultBlock]:
    return [
        ToolResultBlock(tool_use_id=c.id, content=CANCELLED_RESULT, is_error=True) for c in calls
    ]


def _results_message(blocks: list[ToolResultBlock]) -> Message:
    """Tool results always travel as a user message; adapters reshape from there."""
    return Message(role=Role.USER, content=list(blocks))


def unanswered_tool_uses(session: Session) -> list[str]:
    """Tool-use ids with no matching tool_result. Should always be empty.

    A non-empty result means the session cannot be resumed against Anthropic
    or Bedrock, so this is what the cancellation tests assert on.
    """
    answered: set[str] = set()
    requested: list[str] = []
    for message in session.messages:
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                requested.append(block.id)
            elif isinstance(block, ToolResultBlock):
                answered.add(block.tool_use_id)
    return [i for i in requested if i not in answered]


def build_workspace(
    config: Config,
    *,
    root: str | Path | None = None,
    extra_roots: Sequence[str] = (),
) -> Workspace:
    """The workspace for a session: cwd plus any opt-in roots."""
    return Workspace(
        root=Path(root) if root is not None else Path.cwd(),
        extra_roots=tuple(Path(p) for p in (*config.workspace.extra_roots, *extra_roots)),
        deny_secrets=config.workspace.deny_secrets,
    )


def build_tool_context(
    config: Config,
    workspace: Workspace,
    *,
    approvals: ApprovalPolicy | None = None,
    registry: ToolRegistry | None = None,
) -> ToolContext:
    """Defaults to DenyAll, so a caller that forgets a policy cannot write."""
    return ToolContext(
        workspace=workspace,
        approvals=approvals or DenyAll(),
        cloud=build_cloud_context(config),
        max_file_bytes=config.tools.max_file_bytes,
        workflow_settings=config.workflow,
        registry=registry,
    )


def build_cloud_context(config: Config) -> CloudContext:
    """Cluster state for the tools. The client itself is built lazily, so a
    session that never mentions Kubernetes never connects to one."""
    from altus.cloud.base import ProtectionRules, integration
    from altus.mcp.catalog import CATALOG

    MCP_SERVERS = [spec.id for spec in CATALOG]
    settings = config.cloud
    kubeconfigs = tuple(settings.kubeconfigs)
    provider = None
    entry = integration("k8s")
    if entry and entry.available:
        from altus.cloud.k8s import K8sProvider

        provider = K8sProvider(context=settings.kube_context, kubeconfigs=kubeconfigs)

    from altus.cloud.aws import AwsProvider

    aws_provider = AwsProvider(region=settings.default_region)

    azure_provider = None
    azure_entry = integration("azure")
    if azure_entry and azure_entry.available:
        from altus.cloud.azure import AzureProvider

        azure_provider = AzureProvider(
            subscription=settings.azure_subscription or "",
            max_results=settings.azure.max_results,
        )

    gcp_provider = None
    gcp_entry = integration("gcp")
    if gcp_entry and gcp_entry.available:
        from altus.cloud.gcp import GcpProvider

        gcp_provider = GcpProvider(
            project=settings.gcp_project or "", max_results=settings.gcp.max_results
        )

    mcp_provider = None
    mcp_entry = integration("mcp")
    if mcp_entry and mcp_entry.available and config.mcp.enabled:
        from altus.mcp.session import McpProvider

        mcp_provider = McpProvider(
            timeout=config.mcp.timeout,
            max_result_bytes=config.mcp.max_result_bytes,
            scopes={s: config.mcp.for_server(s).scope for s in MCP_SERVERS},
            urls={s: config.mcp.for_server(s).url for s in MCP_SERVERS},
        )

    forwards = None
    if provider is not None and settings.k8s.allow_port_forward:
        from altus.tools.k8s.streams import PortForwards

        forwards = PortForwards()

    def remember(name: str, _namespace: str) -> None:
        """Persist a switch the agent made, so --resume comes back to the same
        cluster. Only the Altus-scoped selection: the kubeconfig is never
        written from here, whatever kube_context_scope says --- a tool call is
        not the place to retarget the user's other terminals."""
        from altus.config import save_config

        settings.kube_context = name
        save_config(config)

    return CloudContext(
        k8s=provider,
        redact_secrets=settings.secret_redaction,
        kubeconfigs=kubeconfigs,
        kube_context=settings.kube_context,
        protection=ProtectionRules.build(
            settings.protected.patterns, settings.protected.accounts, settings.protected.mode
        ),
        on_context_change=remember,
        aws=aws_provider,
        aws_region=settings.default_region or "",
        aws_settings=settings.aws,
        azure=azure_provider,
        azure_subscription=settings.azure_subscription or "",
        azure_settings=settings.azure,
        gcp=gcp_provider,
        gcp_project=settings.gcp_project or "",
        gcp_settings=settings.gcp,
        mcp=mcp_provider,
        mcp_settings=config.mcp,
        exec_timeout=settings.k8s.exec_timeout,
        allow_rbac_writes=settings.k8s.allow_rbac_writes,
        cli_allowlist=tuple(settings.cli_allowlist),
        port_forwards=forwards,
    )
