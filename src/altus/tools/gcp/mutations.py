"""Changes to GCP. Every one is gated.

GCP's preview is the weakest of the four clouds, measured rather than guessed:
**498 of 26,438 methods take a validateOnly or dryRun parameter --- 1.9%**,
against AWS's 4.3% and Azure's real What-If diff. So most of the time the
prompt has nothing to show about *what* would change, and says exactly that.

What it can offer instead is real:

  deletion protection  read from the resource; a refusal, not a warning
  liens                block deleting a project, the most destructive call here
  validateOnly         where the method declares it, the server validates
  testIamPermissions   "may I" --- the most widely available of the four

Never "validated" when nothing was validated: a prompt that buys false
confidence at the moment of consent is worse than one that admits the gap.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import gcp as api
from altus.cloud.base import Sensitivity
from altus.tools.base import ToolContext, ToolOutcome
from altus.tools.gcp.base import GcpMutatingTool

RECOVERABILITY: dict[Sensitivity, str] = {
    Sensitivity.PRIVILEGED: "this cannot be undone from here, and may not be undoable at all",
    Sensitivity.MUTATE: "GCP does not model an undo; reversing this is your own work",
}


class GcpWriteTool(GcpMutatingTool):
    name: ClassVar[str] = "gcp_write"
    action: ClassVar[str] = "call"
    description: ClassVar[str] = (
        "Perform a Google Cloud method that changes something. Requires user "
        "approval every time. Call gcp_explain first for the exact parameter "
        "names and to see whether the method supports a validateOnly dry run — "
        "most do not, and the prompt says so rather than implying a check that "
        "never happened. A resource with deletionProtection set, or a project "
        "with a lien, is refused outright."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "e.g. compute.instances.delete"},
            "params": {"type": "object", "description": "Method parameters, exactly named."},
            "body": {
                "type": "object",
                "description": "The request body, when the method takes one.",
            },
            "reason": {
                "type": "string",
                "description": "Why this change is wanted. Shown to the user.",
            },
        },
        "required": ["method"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        method = str(args.get("method", "")).strip()
        if not method:
            return ToolOutcome.error("method is required, e.g. compute.instances.delete")
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return ToolOutcome.error("params must be an object")

        sensitivity = api.classify(method)
        if sensitivity in (Sensitivity.READ, Sensitivity.SENSITIVE_READ):
            return ToolOutcome.error(
                f"{method} only reads. Use gcp_call, which does not prompt.", summary="wrong tool"
            )
        if api.lookup(method) is None:
            return ToolOutcome.error(
                f"no method {method!r} ships with this client. Call gcp_explain with "
                "`api` alone to see what does.",
                summary="unknown",
            )

        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)

        arguments = dict(params)
        if project and "project" not in arguments and "project" in _parameters(method):
            arguments["project"] = project
        body = args.get("body")
        if isinstance(body, dict):
            arguments["body"] = body

        # Refusals first. A call that will fail is not a decision the user has
        # to make, and prompting for one spends their attention on nothing.
        permitted, protection = await self.check_protection(provider, method, arguments)
        if not permitted:
            return ToolOutcome.error(
                f"{method} would be refused: {protection}. Clear it first — which is "
                "itself a change.",
                summary="protected",
            )
        permitted, lien = await self.check_liens(provider, method, project)
        if not permitted:
            return ToolOutcome.error(f"{method} would be refused: {lien}.", summary="lien")

        preflight = await self._preflight(provider, method, arguments, project, protection)
        if preflight is None:
            return ToolOutcome.error(
                f"{method} would not succeed — the server rejected it during validation.",
                summary="refused",
            )

        reason = str(args.get("reason") or "").strip()
        summary = f"{method}\n{_render(arguments)}"
        summary += f"\nreason given: {reason}" if reason else "\n(no reason given)"

        refused = await self.confirm(
            ctx,
            method_id=method,
            project=project,
            scope=f"projects/{project}" if project else "",
            summary=summary,
            preflight=preflight,
            recoverability=RECOVERABILITY.get(sensitivity, ""),
        )
        if refused is not None:
            return refused

        try:
            payload = await provider.call(method, arguments)
        except Exception as exc:
            return ToolOutcome.error(f"{method} failed: {exc}", summary="failed")
        return ToolOutcome(
            content=f"{method} in {project}\n{_render(self.scrub(payload, ctx))}", summary=method
        )

    async def _preflight(
        self,
        provider: Any,
        method: str,
        arguments: dict[str, Any],
        project: str,
        protection: str,
    ) -> str | None:
        """What could be checked, and the honest name for it.

        None means the server itself refused during validation --- the one case
        where there is nothing to ask about. Everything else returns a sentence
        that goes verbatim into the approval prompt.
        """
        parts: list[str] = []
        validator = api.supports_validate_only(method)
        if validator:
            try:
                await provider.call(method, {**arguments, validator: True})
            except Exception as exc:
                return (
                    None
                    if "invalid" in str(exc).casefold()
                    else (
                        f"validation could not run ({str(exc)[:100]}). "
                        "What this changes is not known in advance."
                    )
                )
            parts.append(
                f"{validator} succeeded: the server accepted this without applying it. "
                "It validates the request, not the outcome."
            )
        else:
            parts.append(
                "no preview exists: this method cannot be validated without running it, "
                "and 98% of Google's methods cannot."
            )

        allowed, detail = await self.check_permission(
            provider, method, f"projects/{project}" if project else ""
        )
        if allowed is True:
            parts.append(f"permission check: you hold {detail}")
        elif allowed is False:
            parts.append(f"permission check: you do NOT hold {detail} — this will probably fail")
        else:
            parts.append(f"permission check could not run ({detail})")

        if protection:
            parts.append(protection)
        if not validator:
            parts.append("What it changes is not known in advance.")
        # Each part is its own sentence; without this they run together into
        # one unreadable line in the prompt, which is where it matters most.
        return " ".join(p if p.endswith(".") else f"{p}." for p in parts)


def _parameters(method_id: str) -> set[str]:
    found = api.lookup(method_id)
    return set((found or {}).get("parameters") or {})


def _render(payload: Any) -> str:
    import yaml

    if not payload:
        return "  (no parameters)"
    text = yaml.safe_dump(payload, default_flow_style=False, sort_keys=True, allow_unicode=True)
    return "\n".join(f"  {line}" for line in str(text).splitlines()[:40])
