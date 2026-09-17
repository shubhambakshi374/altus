"""The tool contract.

Tool failures are *data*, not exceptions: a tool that cannot do its job
returns an error ``ToolOutcome`` which the agent loop hands back to the model
as an error ``tool_result``. Raising into the loop would abort a turn that the
model could have recovered from by trying a different path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from altus.cloud.base import Sensitivity
from altus.core.visuals import Visual
from altus.tools.approval import ApprovalPolicy, DenyAll
from altus.workspace import Workspace

DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_MATCHES = 200


@dataclass
class CloudContext:
    """Cluster and cloud state the tools operate against."""

    k8s: Any = None
    """A ``altus.cloud.k8s.K8sProvider``; typed loosely so ``tools.base`` does
    not import an optional SDK path at module scope."""
    redact_secrets: bool = True
    kubeconfigs: tuple[str, ...] = ()
    kube_context: str | None = None
    protection: Any = None
    """A ``altus.cloud.base.ProtectionRules``."""
    on_context_change: Callable[[str, str], None] | None = None
    """Called after a switch, to persist it. Optional: a headless run that
    should not write config simply leaves it unset."""
    aws: Any = None
    """A ``altus.cloud.aws.AwsProvider``; typed loosely so ``tools.base`` does not
    import an SDK path at module scope."""
    aws_region: str = ""
    aws_settings: Any = None
    """A ``altus.config.models.AwsSettings``. Checked at the gate for the classes
    that cannot be enforced by withholding a tool."""
    azure: Any = None
    """A ``altus.cloud.azure.AzureProvider``; typed loosely so ``tools.base`` does
    not import an optional SDK path at module scope."""
    azure_subscription: str = ""
    azure_settings: Any = None
    """A ``altus.config.models.AzureSettings``. Checked at the gate, for the same
    reason its AWS namesake is: the same azure_write sets a tag and a role
    assignment."""
    gcp: Any = None
    """A ``altus.cloud.gcp.GcpProvider``; typed loosely so ``tools.base`` does
    not import an optional SDK path at module scope."""
    gcp_project: str = ""
    gcp_settings: Any = None
    """A ``altus.config.models.GcpSettings``. Checked at the gate for the same
    reason the other two are: the same gcp_write sets a label and an IAM
    policy."""
    mcp: Any = None
    """An ``altus.mcp.session.McpProvider``; typed loosely so ``tools.base`` does
    not import the MCP SDK at module scope."""
    mcp_settings: Any = None
    """An ``altus.config.models.McpSettings``. Checked at the gate for the same
    reason its cloud namesakes are: the same mcp_do comments on an issue and
    deletes a repository."""
    exec_timeout: int = 60
    cli_allowlist: tuple[str, ...] = ()
    """Binaries the CLI fallback may run. Registration already filters on this,
    but a tool must not depend on having been registered correctly to be safe:
    it is checked again at call time."""
    allow_rbac_writes: bool = True
    """Creating or changing Roles, Bindings, ServiceAccounts and CSRs. Cannot
    be enforced by not registering a tool --- the same k8s_apply writes a
    ConfigMap and a ClusterRoleBinding --- so it is checked at the gate."""
    port_forwards: Any = None
    """A ``altus.tools.k8s.streams.PortForwards``. Built per session so a tunnel
    cannot outlive the session that opened it."""

    def switch_context(self, name: str, namespace: str = "") -> None:
        """Point this session at another cluster.

        The cached client has to go: it holds a connection built for the old
        context, and reusing it would send the next call to the cluster the
        user just moved away from.
        """
        self.kube_context = name
        provider = self.k8s
        if provider is not None:
            provider.context = name
            provider.reset()
        if self.on_context_change is not None:
            self.on_context_change(name, namespace)


@dataclass
class ToolContext:
    """What a tool is allowed to touch, and what it must ask before doing.

    ``approvals`` defaults to ``DenyAll``: a caller that forgets to wire a
    policy gets refusals, not silent writes.
    """

    workspace: Workspace
    approvals: ApprovalPolicy = field(default_factory=DenyAll)
    cloud: CloudContext = field(default_factory=lambda: CloudContext())
    workflow_settings: Any = None
    """An ``altus.config.models.WorkflowSettings``. Not under ``cloud``: a
    workflow is not a cloud, and MCP's settings only live there because they
    were put there before that distinction was worth making."""
    registry: Any = None
    """The session's ``ToolRegistry``, for the one tool that has to reason
    about the others. Typed loosely because ``tools.registry`` imports this
    module. None means a caller that never needed it."""
    shell_settings: Any = None
    """An ``altus.config.models.ShellSettings``. Read at call time as well as
    at registration, for the reason ``cli_allowlist`` is: a tool must not
    depend on having been registered correctly in order to be safe."""
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_lines: int = DEFAULT_MAX_LINES
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_matches: int = DEFAULT_MAX_MATCHES


@dataclass
class ToolOutcome:
    """A tool's result: what the model sees, plus a label for the UI."""

    content: str
    is_error: bool = False
    summary: str = ""
    denied: bool = False
    """True when the user rejected it, as opposed to the tool failing."""
    visual: Visual | None = None
    """For the human only. Never reaches the model --- that is the point:
    a cluster topology renders richly while costing no context."""

    @classmethod
    def error(cls, message: str, *, summary: str = "") -> ToolOutcome:
        return cls(content=message, is_error=True, summary=summary or "failed")

    @classmethod
    def rejected(cls, message: str) -> ToolOutcome:
        return cls(content=message, is_error=True, summary="rejected", denied=True)


@runtime_checkable
class Tool(Protocol):
    """What the registry, the agent loop and the Phase 2 engine may assume."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any]: ...

    @property
    def read_only(self) -> bool: ...

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome: ...


class BaseTool(ABC):
    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    read_only: ClassVar[bool] = True
    input_schema: ClassVar[dict[str, Any]] = {}

    dispatches: ClassVar[bool] = False
    """True when the real sensitivity depends on the call's arguments.

    ``aws_write`` is one tool that reaches 19,189 operations; ``k8s_apply``
    writes a ConfigMap and a ClusterRoleBinding. For those, anything decided
    before the arguments exist is a floor and not an answer, and a caller
    reasoning about a call that has not been made yet has to be told so.
    """

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """Do the work. Return an error outcome rather than raising."""

    @classmethod
    def static_sensitivity(cls) -> Sensitivity:
        """The strictest level knowable without the arguments.

        Each surface overrides this where it knows better --- the Kubernetes
        tools run their own verb and subresource through the Kubernetes
        classifier --- because a verb means different things on different
        surfaces. Azure also calls its verbs ``write`` and ``action``, and
        putting those through the Kubernetes classifier is how ``/tools``
        came to report every Azure mutation as privileged.
        """
        return Sensitivity.READ if cls.read_only else Sensitivity.MUTATE


def sensitivity_of(tool: Any) -> Sensitivity:
    """How dangerous a tool is, before anyone has chosen its arguments.

    One function because there are two doors onto it --- ``/tools`` and the
    workflow designer --- and two doors deciding separately is how the same
    question gets two answers.
    """
    found = getattr(type(tool), "static_sensitivity", None)
    if found is None:
        # A Tool satisfying the Protocol without subclassing BaseTool. The
        # read/write flag is all such a tool promises, so it is all we use.
        return Sensitivity.READ if getattr(tool, "read_only", True) else Sensitivity.MUTATE
    return Sensitivity(found())


def dispatches(tool: Any) -> bool:
    """Whether the arguments could still make this stricter than it looks.

    ``PRIVILEGED`` is the top of the scale, so a tool already there has
    nothing left to escalate to and is never reported as unresolved --- which
    is what keeps ``k8s_exec`` out of the list even though it shares a base
    class with ``k8s_apply``.
    """
    if not getattr(type(tool), "dispatches", False):
        return False
    return sensitivity_of(tool) is not Sensitivity.PRIVILEGED


def truncated_note(shown: int, total: int, unit: str) -> str:
    return f"\n\n[truncated: showing {shown} of {total} {unit}]"
