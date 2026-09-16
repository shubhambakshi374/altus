"""Shared plumbing for every GCP tool.

Resolving a session, naming the blast radius, and the gate every change passes
through. GCP's preview is the weakest of the four clouds --- 1.9% of methods
take a validateOnly parameter, against AWS's 4.3% and Azure's real What-If
diff --- so what goes in the prompt is assembled from whatever *could* be
checked, and says plainly which of those it was.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import gcp as api
from altus.cloud.base import ProtectionMode, Sensitivity
from altus.cloud.redact import redact
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome


class GcpTool(BaseTool):
    """Resolve a provider, and refuse clearly when there is none."""

    read_only: ClassVar[bool] = True

    async def provider(self, ctx: ToolContext) -> Any | ToolOutcome:
        found = getattr(ctx.cloud, "gcp", None)
        if found is None:
            return ToolOutcome.error(
                "no GCP session is configured. Install the extra with "
                "`uv sync --extra gcp`, then /login gcp.",
                summary="unavailable",
            )
        return found

    async def identity(self, ctx: ToolContext, provider: Any) -> dict[str, str] | ToolOutcome:
        try:
            return dict(await provider.whoami())
        except Exception as exc:
            return ToolOutcome.error(
                f"could not resolve Google credentials: {exc}. Check /login gcp.",
                summary="unreachable",
            )

    def scrub(self, payload: Any, ctx: ToolContext) -> Any:
        return redact(payload, enabled=ctx.cloud.redact_secrets)

    def project_for(self, args: dict[str, Any], ctx: ToolContext, provider: Any) -> str:
        """One project, always.

        Cloud Asset Inventory could sweep a whole organisation in a single
        call, but then a read silently spans production and the CloudTarget in
        a prompt no longer describes what was touched. Widening is an explicit
        argument on the tools that offer it --- the same decision the Azure
        subscription scope took.
        """
        return str(
            args.get("project")
            or getattr(ctx.cloud, "gcp_project", "")
            or provider.default_project()
            or ""
        )


#: Fields that mean "this resource refuses to be deleted". Read before a
#: delete, they are GCP's nearest equivalent to an Azure resource lock: a
#: genuine "this call will fail" signal rather than a guess.
PROTECTION_FIELDS = ("deletionProtection", "deletionProtectionEnabled", "enableDeletionProtection")


class GcpMutatingTool(GcpTool):
    """Resolve, preflight, classify, ask, act. Nothing is sent before the
    approval returns."""

    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "call"
    dispatches: ClassVar[bool] = True

    async def check_protection(
        self, provider: Any, method_id: str, params: dict[str, Any]
    ) -> tuple[bool, str]:
        """Whether a delete would be refused by the resource itself.

        Best-effort on purpose: plenty of identities may delete a resource and
        not read it, and treating "I could not check" as "it is protected"
        would make the tool useless on those. A failure comes back permitted,
        with the reason named.
        """
        if method_id.rsplit(".", 1)[-1] not in api.DESTROY_VERBS:
            return True, ""
        getter = f"{method_id.rsplit('.', 1)[0]}.get"
        if api.lookup(getter) is None:
            return True, "no matching read, so deletion protection could not be checked"
        try:
            current = await provider.call(getter, dict(params))
        except Exception as exc:
            return True, f"deletion protection could not be checked ({str(exc)[:80]})"
        for field in PROTECTION_FIELDS:
            if current.get(field):
                return False, f"{field} is set on this resource"
        return True, "no deletion protection is set"

    async def check_liens(self, provider: Any, method_id: str, project: str) -> tuple[bool, str]:
        """Liens block deleting a project, which is the most destructive call
        available here. Nothing else in GCP is lien-protected, so this is asked
        only where it applies."""
        if not method_id.startswith("cloudresourcemanager.projects.delete"):
            return True, ""
        try:
            payload = await provider.call(
                "cloudresourcemanager.liens.list", {"parent": f"projects/{project}"}
            )
        except Exception as exc:
            return True, f"liens could not be listed ({str(exc)[:80]})"
        liens = payload.get("liens") or []
        if not liens:
            return True, "no lien applies"
        first = liens[0]
        return False, (
            f"a lien named {first.get('name', '?')} blocks this "
            f"({first.get('reason', 'no reason given')})"
        )

    async def check_permission(
        self, provider: Any, method_id: str, resource: str
    ) -> tuple[bool | None, str]:
        """testIamPermissions --- "may I", not "what changes".

        The most widely available of the four clouds' equivalents: declared on
        598 resources, and asking about yourself needs no extra permission,
        unlike AWS's SimulatePrincipalPolicy.
        """
        permission = api.permission_for(method_id)
        try:
            held = await provider.test_permissions(method_id, resource)
        except Exception as exc:
            return None, str(exc)[:120]
        if not permission:
            return None, "the permission could not be derived from this method"
        return permission in held, permission

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        method_id: str,
        project: str,
        scope: str,
        summary: str,
        preflight: str,
        recoverability: str = "",
    ) -> ToolOutcome | None:
        """None means go ahead."""
        target = api.target_for(project)
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(target))
        if protected and getattr(rules, "mode", None) == ProtectionMode.DENY:
            return ToolOutcome.error(
                f"{project} is protected and [cloud.protected] mode is 'deny', "
                "so changes are refused outright.",
                summary="protected",
            )

        sensitivity = api.classify(method_id, scope)
        if not _writes_allowed(ctx, method_id, sensitivity):
            return ToolOutcome.error(
                f"{method_id} is disabled by [cloud.gcp] configuration.", summary="disabled"
            )

        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=method_id,
                target=f"project {project}",
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


def _writes_allowed(ctx: ToolContext, method_id: str, sensitivity: Sensitivity) -> bool:
    """The [cloud.gcp] switches that cannot work by withholding a tool.

    The same gcp_write sets a label and a bucket's IAM policy, so these are
    checked here rather than at registration --- exactly as allow_rbac_writes
    is for Kubernetes and Azure, and allow_iam_writes for AWS.
    """
    settings = getattr(ctx.cloud, "gcp_settings", None)
    if settings is None:
        return True
    if not getattr(settings, "allow_writes", True):
        return False
    service = method_id.split(".", 1)[0]
    verb = method_id.rsplit(".", 1)[-1]
    identity_write = (
        verb == "setIamPolicy"
        or method_id in api.CREDENTIAL_MINTING
        or service in api.PRIVILEGED_APIS
    )
    if identity_write and not getattr(settings, "allow_iam_writes", True):
        return False
    return not (verb in api.DESTROY_VERBS and not getattr(settings, "allow_delete", True))
