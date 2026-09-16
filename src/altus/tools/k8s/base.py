"""Shared plumbing for every Kubernetes tool.

Resolving a client, scrubbing what comes back, and the approval gate every
mutation passes through: dry run, classify, ask, then act.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import k8s as k8s_api
from altus.cloud.base import ProtectionMode, Sensitivity
from altus.cloud.redact import redact
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome

MAX_LOG_LINES = 400


class K8sTool(BaseTool):
    """Shared plumbing: resolve a client, and refuse clearly when we cannot."""

    read_only: ClassVar[bool] = True

    async def client(self, ctx: ToolContext) -> tuple[k8s_api.K8sClient, str] | ToolOutcome:
        provider = ctx.cloud.k8s
        if provider is None:
            return ToolOutcome.error(
                "no Kubernetes client is configured for this session", summary="unavailable"
            )
        try:
            got: tuple[k8s_api.K8sClient, str] = await provider.get()
            return got
        except Exception as exc:
            return ToolOutcome.error(
                f"could not reach the cluster: {exc}. Check /kube and /login.",
                summary="unreachable",
            )

    def scrub(self, payload: Any, ctx: ToolContext) -> Any:
        return redact(payload, enabled=ctx.cloud.redact_secrets)


def _rows(items: list[dict[str, Any]], columns: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for item in items:
        meta = item.get("metadata") or {}
        out.append(
            [
                str(meta.get("name", "")),
                str(meta.get("namespace", "")),
                k8s_api.status_for(item.get("kind", ""), item),
                str(meta.get("creationTimestamp", ""))[:19].replace("T", " "),
            ][: len(columns)]
        )
    return out


class K8sMutatingTool(K8sTool):
    """Shared shape for every change: resolve, dry-run, ask, then act.

    Nothing is applied before the approval returns. The dry-run result is what
    goes in the prompt, so the user is shown what the *server* says will
    happen rather than what the model claims will happen.
    """

    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "change"
    verb: ClassVar[str] = "update"
    """The API verb this tool performs, for classification. Not the same as
    ``action``, which is the word shown to the user."""
    subresource: ClassVar[str] = ""
    """The subresource this tool always acts on --- ``exec``, ``portforward``.
    Classification reads it, so leaving it unset silently downgrades a tool to
    whatever its verb alone implies."""
    dispatches: ClassVar[bool] = True
    """The kind comes from the arguments, and the kind is half the answer:
    ``classify`` in ``confirm`` below reads it, so the same ``k8s_apply``
    writes a ConfigMap and a ClusterRoleBinding."""

    @classmethod
    def static_sensitivity(cls) -> Sensitivity:
        from altus.cloud.kube import classify

        return classify(cls.verb, "", cls.subresource)

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        client: Any,
        context_name: str,
        kind: str,
        name: str,
        namespace: str,
        diff: str,
        dry_run: str,
        recoverability: str = "",
        verb: str = "",
        subresource: str = "",
    ) -> ToolOutcome | None:
        """None means go ahead.

        ``verb`` and ``subresource`` override the class defaults for tools whose
        sensitivity varies per call --- ``k8s_delete`` doing a deletecollection,
        ``k8s_create`` creating an eviction.
        """
        from altus.cloud.kube import KubeContext, classify

        target = KubeContext(name=context_name, namespace=namespace).target(namespace)
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(target))
        if protected and getattr(rules, "mode", None) == ProtectionMode.DENY:
            return ToolOutcome.error(
                f"{context_name} is a protected context and [cloud.protected] mode is "
                "'deny', so changes are refused outright.",
                summary="protected",
            )

        sensitivity = classify(verb or self.verb, kind, subresource or self.subresource)
        if not getattr(ctx.cloud, "allow_rbac_writes", True) and _is_rbac_write(
            verb or self.verb, kind, subresource or self.subresource
        ):
            return ToolOutcome.error(
                f"writing {kind} is disabled: [cloud.k8s] allow_rbac_writes is false. "
                "This one cannot be enforced by withholding a tool --- the same k8s_apply "
                "writes a ConfigMap --- so it is refused here instead.",
                summary="rbac writes off",
            )
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=f"{kind}/{name}" if name else kind,
                target=f"cluster {context_name} · namespace {namespace or '-'}",
                diff=diff,
                dry_run=dry_run,
                recoverability=recoverability,
                destructive=True,
                protected=protected,
                sensitivity=sensitivity,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected("The user rejected this change.")
        return None


def _is_rbac_write(verb: str, kind: str, subresource: str) -> bool:
    """Does this change who may do what, or mint a credential?"""
    from altus.cloud.kube import MUTATE_VERBS

    if subresource.casefold() in {"token", "approval", "escalate", "impersonate", "binding"}:
        return True
    return verb.casefold() in MUTATE_VERBS and kind in RBAC_KINDS


#: The subset of PRIVILEGED_KINDS that is specifically about authorization.
#: Draining a node is privileged too, but it is not an RBAC write.
RBAC_KINDS = frozenset(
    {
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
        "ServiceAccount",
        "CertificateSigningRequest",
    }
)
