"""Shared plumbing for every Azure tool.

Resolving a session, naming the blast radius, and the gate every change passes
through. Azure's gate can promise more than the AWS one: where AWS could
preview 4.3% of its operations and the prompt had to admit the gap, Azure has
three real mechanisms --- a server-side What-If diff, resource locks, and an
RBAC check --- and the prompt says which of them actually ran.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import azure as arm
from altus.cloud.base import ProtectionMode, Sensitivity
from altus.cloud.redact import redact
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome


class AzureTool(BaseTool):
    """Resolve a provider, and refuse clearly when there is none."""

    read_only: ClassVar[bool] = True

    async def provider(self, ctx: ToolContext) -> Any | ToolOutcome:
        found = getattr(ctx.cloud, "azure", None)
        if found is None:
            return ToolOutcome.error(
                "no Azure session is configured. Install the extra with "
                "`uv sync --extra azure`, then /login azure.",
                summary="unavailable",
            )
        return found

    async def identity(self, ctx: ToolContext, provider: Any) -> dict[str, str] | ToolOutcome:
        try:
            return dict(await provider.whoami())
        except Exception as exc:
            return ToolOutcome.error(
                f"could not authenticate to Azure: {exc}. Check /login azure.",
                summary="unreachable",
            )

    def scrub(self, payload: Any, ctx: ToolContext) -> Any:
        return redact(payload, enabled=ctx.cloud.redact_secrets)

    def subscription_for(self, args: dict[str, Any], ctx: ToolContext, provider: Any) -> str:
        """One subscription, always.

        A credential commonly sees many, and Resource Graph could sweep all of
        them in one call --- but then a read silently spans production and the
        CloudTarget in a prompt no longer describes what was touched. Widening
        is an explicit argument on the tools that offer it.
        """
        return str(
            args.get("subscription")
            or getattr(ctx.cloud, "azure_subscription", "")
            or getattr(provider, "subscription", "")
            or ""
        )


class AzureMutatingTool(AzureTool):
    """Resolve, preflight, classify, ask, act. Nothing is sent before the
    approval returns."""

    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "call"
    dispatches: ClassVar[bool] = True

    async def check_lock(self, provider: Any, scope: str) -> tuple[bool, str]:
        """Locks that apply at or above this scope.

        Azure's own answer to "would this actually work", and a mechanism AWS
        has no equivalent of. A ``CanNotDelete`` lock means the call will fail,
        so it is reported as a refusal before anyone is asked --- prompting for
        a decision that does not exist spends the user's attention for nothing.

        A failure to *read* the locks is not a refusal: plenty of working
        identities cannot list them, and treating "I could not check" as "it is
        locked" would make the tool useless on those.
        """
        try:
            found = await provider.locks(scope)
        except Exception as exc:
            return True, f"lock check could not run ({str(exc)[:100]})"
        blocking = [lock for lock in found if lock.get("level") in {"CanNotDelete", "ReadOnly"}]
        if not blocking:
            return True, "no lock applies"
        first = blocking[0]
        return False, (
            f"a {first['level']} lock named {first['name']!r} applies at "
            f"{first.get('scope') or scope}"
        )

    async def check_access(
        self, provider: Any, scope: str, operation: str
    ) -> tuple[bool | None, str]:
        """RBAC's "may I". None means the check itself could not run."""
        if not operation:
            return None, "the operation could not be derived from this resource id"
        try:
            decisions = await provider.check_access(scope, [operation])
        except Exception as exc:
            return None, str(exc)[:120]
        if not decisions:
            return None, "checkAccess returned nothing"
        verdict = decisions[0].get("decision", "")
        return verdict.casefold() == "allowed", verdict or "no decision"

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        operation: str,
        scope: str,
        subscription: str,
        region: str,
        group: str,
        summary: str,
        preflight: str,
        recoverability: str = "",
    ) -> ToolOutcome | None:
        """None means go ahead."""
        target = arm.target_for(subscription, region, group)
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(target))
        if protected and getattr(rules, "mode", None) == ProtectionMode.DENY:
            return ToolOutcome.error(
                f"{subscription} is protected and [cloud.protected] mode is 'deny', "
                "so changes are refused outright.",
                summary="protected",
            )

        sensitivity = arm.classify(operation, scope)
        if not _writes_allowed(ctx, operation, sensitivity):
            return ToolOutcome.error(
                f"{operation or 'this operation'} is disabled by [cloud.azure] configuration.",
                summary="disabled",
            )

        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=operation or scope,
                target=f"subscription {subscription}" + (f" · {group}" if group else ""),
                diff=summary,
                dry_run=preflight,
                recoverability=recoverability,
                destructive=True,
                protected=protected,
                sensitivity=sensitivity,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected("The user rejected this call.")
        return None


def _writes_allowed(ctx: ToolContext, operation: str, sensitivity: Sensitivity) -> bool:
    """The [cloud.azure] switches that cannot work by withholding a tool.

    The same azure_write sets a tag and a role assignment, so these are checked
    here rather than at registration --- exactly as allow_rbac_writes is for
    Kubernetes and allow_iam_writes is for AWS.
    """
    settings = getattr(ctx.cloud, "azure_settings", None)
    if settings is None:
        return True
    if not getattr(settings, "allow_writes", True):
        return False
    parsed = arm.parse_operation(operation)
    if parsed is None:
        # Unparseable reached the gate, which classify already called
        # PRIVILEGED. Refuse rather than guess which switch covers it.
        return False
    if parsed.namespace.casefold() in arm.PRIVILEGED_NAMESPACES and not getattr(
        settings, "allow_rbac_writes", True
    ):
        return False
    return not (parsed.verb == "delete" and not getattr(settings, "allow_delete", True))
