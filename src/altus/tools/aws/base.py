"""Shared plumbing for every AWS tool.

Resolving a session, naming the blast radius, and the approval gate every
mutation passes through. The Kubernetes equivalent dry-runs against the API
server and shows its verdict; AWS offers that for 4.3% of its operations, so
what goes in the prompt here is assembled from whatever *could* be checked and
says plainly which of those it was.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import aws as aws_api
from altus.cloud.base import ProtectionMode
from altus.cloud.redact import redact
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome


class AwsTool(BaseTool):
    """Resolve a provider, and refuse clearly when there is none."""

    read_only: ClassVar[bool] = True

    async def provider(self, ctx: ToolContext) -> Any | ToolOutcome:
        found = getattr(ctx.cloud, "aws", None)
        if found is None:
            return ToolOutcome.error(
                "no AWS session is configured. Check /login aws.", summary="unavailable"
            )
        return found

    async def identity(self, ctx: ToolContext, provider: Any) -> dict[str, str] | ToolOutcome:
        try:
            return dict(await provider.whoami())
        except Exception as exc:
            return ToolOutcome.error(
                f"could not reach AWS: {exc}. Check /login aws.", summary="unreachable"
            )

    def scrub(self, payload: Any, ctx: ToolContext) -> Any:
        return redact(payload, enabled=ctx.cloud.redact_secrets)

    def region_for(self, args: dict[str, Any], ctx: ToolContext, provider: Any) -> str:
        configured = getattr(ctx.cloud, "aws_region", "") or ""
        return str(args.get("region") or configured or provider.default_region())


class AwsMutatingTool(AwsTool):
    """Resolve, preflight, classify, ask, act. Nothing is sent before the
    approval returns."""

    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "call"
    dispatches: ClassVar[bool] = True

    async def preflight(
        self, provider: Any, service: str, operation: str, params: dict[str, Any], region: str
    ) -> tuple[bool, str]:
        """What could be checked, and the honest name for it.

        Returns ``(permitted, description)``. The description goes verbatim
        into the approval prompt, so it must never imply a dry run that did not
        happen --- a prompt that buys false confidence at the moment of consent
        is worse than one that admits the gap.
        """
        if _supports_dry_run(service, operation):
            try:
                await provider.call(service, operation, {**params, "DryRun": True}, region=region)
            except Exception as exc:
                if "DryRunOperation" in str(exc):
                    return True, "server-side dry run succeeded: AWS permits this"
                if "UnauthorizedOperation" in str(exc):
                    return False, "server-side dry run: you are not authorised for this"
                return False, f"server-side dry run failed: {exc}"
            # A DryRun that returns normally means the parameter was ignored.
            return True, "dry run accepted, but AWS did not report a DryRunOperation"

        allowed, reason = await _simulate(provider, service, operation, region)
        if allowed is None:
            return True, (
                "not dry-run: AWS cannot preview this operation, and the permission "
                f"check could not run ({reason})"
            )
        if not allowed:
            return False, f"iam:SimulatePrincipalPolicy says this is denied ({reason})"
        return True, (
            "not dry-run: AWS cannot preview this operation. "
            "permission check: allowed. What it will change is not known in advance."
        )

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        service: str,
        operation: str,
        account: str,
        region: str,
        summary: str,
        preflight: str,
        recoverability: str = "",
        before: str = "",
    ) -> ToolOutcome | None:
        """None means go ahead."""
        target = aws_api.target_for(account, region, service)
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(target))
        if protected and getattr(rules, "mode", None) == ProtectionMode.DENY:
            return ToolOutcome.error(
                f"{account} · {region} is protected and [cloud.protected] mode is "
                "'deny', so changes are refused outright.",
                summary="protected",
            )

        sensitivity = aws_api.classify(service, operation)
        if not _writes_allowed(ctx, service, operation, sensitivity):
            return ToolOutcome.error(
                f"{service}:{operation} is disabled by [cloud.aws] configuration.",
                summary="disabled",
            )

        diff = summary if not before else f"{summary}\n\ncurrent state:\n{before}"
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=f"{service}:{operation}",
                target=f"account {account} · {region}",
                diff=diff,
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


def _supports_dry_run(service: str, operation: str) -> bool:
    """Whether the operation's own input shape carries DryRun.

    Asked of botocore rather than assumed from the service name: 758 of EC2's
    802 operations take it and 44 do not, and guessing wrong here means either
    a skipped preflight or a spurious parameter error.
    """
    try:
        shape = aws_api.input_shape(service, operation)
    except Exception:
        return False
    return shape is not None and "DryRun" in shape


async def _simulate(
    provider: Any, service: str, operation: str, region: str
) -> tuple[bool | None, str]:
    """iam:SimulatePrincipalPolicy --- *may I*, not *what changes*.

    ``None`` means the check itself could not run, which is common: simulating
    requires iam:SimulatePrincipalPolicy, and plenty of roles that can do the
    thing cannot ask whether they can. That is reported rather than treated as
    a refusal.
    """
    try:
        identity = await provider.whoami()
        result = await provider.call(
            "iam",
            "SimulatePrincipalPolicy",
            {
                "PolicySourceArn": identity["arn"],
                "ActionNames": [f"{service}:{operation}"],
            },
            region=region,
        )
    except Exception as exc:
        return None, str(exc)[:120]

    results = result.get("EvaluationResults") or []
    if not results:
        return None, "the simulation returned nothing"
    verdict = str(results[0].get("EvalDecision", ""))
    return verdict == "allowed", verdict or "no decision"


def _writes_allowed(ctx: ToolContext, service: str, operation: str, sensitivity: Any) -> bool:
    """The [cloud.aws] switches that cannot work by withholding a tool.

    The same aws_call writes a tag and a trust policy, so these are checked
    here rather than at registration --- exactly as allow_rbac_writes is for
    Kubernetes.
    """
    settings = getattr(ctx.cloud, "aws_settings", None)
    if settings is None:
        return True
    if not getattr(settings, "allow_writes", True):
        return False
    if service in aws_api.PRIVILEGED_SERVICES and not getattr(settings, "allow_iam_writes", True):
        return False
    return not (
        aws_api.IRREVERSIBLE_PATTERN.match(operation)
        and not getattr(settings, "allow_delete", True)
    )
