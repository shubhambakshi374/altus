"""The approval gate for tools that change things.

Read-only tools never reach this. Every mutating tool must get a ``Decision``
before it touches disk, and the default policy is ``DenyAll`` --- failing
closed matters more than convenience when the caller forgot to wire a policy.

The escalation path (``ALLOW_ALWAYS``) is where safety actually erodes, so it
is deliberately narrow: scoped to one tool, held in memory for one session,
never written to disk, and visible in the UI while it is active.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from altus.cloud.base import Sensitivity


class Decision(StrEnum):
    ALLOW = "allow"
    ALLOW_ALWAYS = "allow_always"
    """Allow this, and every later call to the same tool this session."""
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything a human needs to decide, assembled before anything happens."""

    tool: str
    action: str
    """Short verb for the prompt: create, overwrite, edit, delete."""
    path: str
    """The subject: a workspace-relative file, or `deployment/web`."""
    target: str = ""
    """The blast radius: `aws 123456789012 · eu-west-1`, `cluster prod-eu · ns default`.

    "delete deployment web" is meaningless without "in cluster prod-eu-west",
    so anything acting on a remote system must fill this in.
    """
    diff: str = ""
    """Unified diff, or a summary when a diff makes no sense."""
    dry_run: str = ""
    """What the server says would happen, where the API supports asking."""
    recoverability: str = ""
    """Whether this could be undone. The key fact for a delete."""
    destructive: bool = False
    protected: bool = False
    """The target matched a protected-context rule; the UI must demand more
    than a keypress."""
    sensitivity: Sensitivity = Sensitivity.MUTATE
    """Where this sits on the four-level scale. ``PRIVILEGED`` --- running code
    in a container, minting a credential, rewriting RBAC, draining a node ---
    demands the typed challenge and can never be granted standing approval."""

    @property
    def summary(self) -> str:
        where = f" in {self.target}" if self.target else ""
        return f"{self.action} {self.path}{where}"

    @property
    def needs_challenge(self) -> bool:
        """Typing the target's name, rather than a keypress, is required."""
        return self.protected or self.sensitivity.needs_challenge

    @property
    def may_grant_always(self) -> bool:
        """Whether ``allow always`` may even be offered for this request."""
        return not self.needs_challenge


@runtime_checkable
class ApprovalPolicy(Protocol):
    async def request(self, req: ApprovalRequest) -> Decision: ...


class DenyAll:
    """The default. A tool with no policy wired must not be able to write."""

    reason = "no approval policy is configured, so changes are refused"

    async def request(self, req: ApprovalRequest) -> Decision:
        return Decision.DENY


class Parked(Exception):
    """Nobody was there to answer, so the run stopped instead of guessing.

    Deliberately an exception rather than a ``Decision``. A denial is an answer
    --- somebody looked and said no --- and a run that treats "nobody was
    asked" as a denial records a decision that was never made. This unwinds out
    through the tool, which is the only way the engine can tell the two apart.

    It inherits from ``Exception`` directly and not from anything
    ``ToolRegistry.execute`` catches, so it reaches the engine rather than
    becoming an error string in a tool result.
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(f"{request.tool} needs approval, and this run is unattended")
        self.request = request


@dataclass
class ParkOnApproval:
    """The unattended policy: stop at the gate and wait for a person.

    What a triggered run does when it reaches something that needs approval.
    Not ``DenyAll``, which would record a refusal nobody made, and emphatically
    not ``AllowAll``, which would make a trigger a way to launder consent.
    """

    async def request(self, req: ApprovalRequest) -> Decision:
        raise Parked(req)


class AllowAll:
    """Non-interactive consent: `--yes`, and tests."""

    async def request(self, req: ApprovalRequest) -> Decision:
        return Decision.ALLOW


@dataclass
class RecordingPolicy:
    """AllowAll that remembers what it was asked. For tests."""

    decision: Decision = Decision.ALLOW
    seen: list[ApprovalRequest] = field(default_factory=list)

    async def request(self, req: ApprovalRequest) -> Decision:
        self.seen.append(req)
        return self.decision


class SessionApprovals:
    """Wraps a policy with the session-scoped ``always allow`` bookkeeping.

    Also serializes prompts. Without the lock, two writes in one turn would
    race to put a modal on screen; the agent loop already runs mutating tools
    sequentially, and this is the second belt.
    """

    def __init__(self, delegate: ApprovalPolicy) -> None:
        self._delegate = delegate
        self._always: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def always_allowed(self) -> frozenset[str]:
        return frozenset(self._always)

    def revoke_all(self) -> None:
        self._always.clear()

    async def request(self, req: ApprovalRequest) -> Decision:
        # A standing grant is keyed by tool name, but sensitivity is decided
        # per call: `k8s_patch` allowed-always for a label edit must not carry
        # over to the same tool creating an eviction. So a privileged request
        # ignores the cache on the way in and refuses to fill it on the way out.
        grantable = req.may_grant_always
        if grantable and req.tool in self._always:
            return Decision.ALLOW
        async with self._lock:
            # Re-check: an earlier queued prompt may have granted it.
            if grantable and req.tool in self._always:
                return Decision.ALLOW
            decision = await self._delegate.request(req)
        if decision is Decision.ALLOW_ALWAYS:
            if not grantable:
                # The UI should never have offered it. Honour the approval for
                # this one call and drop the standing part on the floor.
                return Decision.ALLOW
            self._always.add(req.tool)
            return Decision.ALLOW
        return decision
