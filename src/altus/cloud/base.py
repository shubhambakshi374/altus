"""Shared cloud primitives: sensitivity, targets, and protected contexts.

Headless --- no Textual, no provider SDK imported at module scope. Phase 3's
workflow engine drives all of this without a terminal.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Sensitivity(StrEnum):
    """Four levels, not two.

    A read/mutate split calls ``sts:GetSessionToken`` a read and lets the model
    fetch live credentials without anyone being asked. Anything that returns
    secret material is its own class.

    ``PRIVILEGED`` is the fourth because the Kubernetes verb alone lies about
    the blast radius: ``get pods`` is a read, and ``get pods/exec`` is arbitrary
    code execution inside a container. Both are ``get``. Anything that runs
    code, mints a credential, rewrites who may do what, or takes a node out of
    service belongs here rather than in ``MUTATE``.
    """

    READ = "read"
    SENSITIVE_READ = "sensitive_read"
    MUTATE = "mutate"
    PRIVILEGED = "privileged"

    @property
    def needs_approval(self) -> bool:
        return self is not Sensitivity.READ

    @property
    def needs_challenge(self) -> bool:
        """Demand the target's name typed out, never a single keypress.

        Also the flag that forbids a standing ``allow always`` grant: one on
        ``k8s_exec`` would be indistinguishable from having no gate at all.
        """
        return self is Sensitivity.PRIVILEGED


class ProtectionMode(StrEnum):
    CONFIRM = "confirm"
    """Require the target's name to be typed, not a keypress."""
    DENY = "deny"


@dataclass(frozen=True)
class CloudTarget:
    """Where an operation would land. Always shown before it happens."""

    cloud: str
    """k8s, aws, azure, gcp."""
    context: str = ""
    """Kube context, AWS account id, Azure subscription, GCP project."""
    location: str = ""
    """Region, or the cluster a kube context points at."""
    scope: str = ""
    """Namespace, resource group, or similar."""

    def render(self) -> str:
        parts = [p for p in (self.context, self.location, self.scope) if p]
        return f"{self.cloud}: " + " · ".join(parts) if parts else self.cloud

    def __str__(self) -> str:
        return self.render()

    @property
    def match_keys(self) -> tuple[str, ...]:
        """Everything a protection pattern is tested against."""
        return tuple(p for p in (self.context, self.location, self.scope) if p)


@dataclass(frozen=True)
class ProtectionRules:
    """Which targets are too important to change on a single keypress."""

    patterns: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    mode: ProtectionMode = ProtectionMode.CONFIRM

    def matches(self, target: CloudTarget) -> bool:
        """Case-insensitive: a real cluster is as likely to be `AKS_EU_PROD`
        as `prod-eu`, and a rule that misses on case is worse than no rule."""
        keys = [k.casefold() for k in target.match_keys]
        for pattern in self.patterns:
            lowered = pattern.casefold()
            if any(fnmatch.fnmatch(key, lowered) for key in keys):
                return True
        return any(account in target.match_keys for account in self.accounts)

    @classmethod
    def build(cls, patterns: Sequence[str], accounts: Sequence[str], mode: str) -> ProtectionRules:
        return cls(tuple(patterns), tuple(accounts), ProtectionMode(mode))


@dataclass
class Integration:
    """One optional capability, and whether its SDK is actually installed."""

    name: str
    extra: str
    modules: tuple[str, ...]
    summary: str
    is_cloud: bool = True
    """Whether `/login` and `auth.status` know how to talk about it. MCP is an
    integration with an extra to install and no cloud to sign in to, so it
    appears under `/tools` but never under `/login`."""
    _available: bool | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = _all_importable(self.modules)
        return self._available

    @property
    def install_hint(self) -> str:
        # Not `altus[extra]`: Altus is not on PyPI, so that command fails. These
        # two are what actually work from a checkout today.
        return f"uv sync --extra {self.extra}   (or: uv tool install '.[{self.extra}]')"


def _all_importable(modules: tuple[str, ...]) -> bool:
    import importlib.util

    for module in modules:
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except ImportError, ValueError:
            return False
    return True


INTEGRATIONS: tuple[Integration, ...] = (
    Integration("k8s", "k8s", ("kubernetes",), "Kubernetes clusters"),
    Integration("aws", "aws", ("boto3", "botocore"), "AWS accounts"),
    Integration("azure", "azure", ("azure.identity",), "Azure subscriptions"),
    Integration("gcp", "gcp", ("googleapiclient", "google.auth"), "Google Cloud projects"),
    Integration(
        "mcp",
        "mcp",
        ("mcp",),
        "MCP servers (GitHub, Jira, Grafana, ...)",
        is_cloud=False,
    ),
)


def integration(name: str) -> Integration | None:
    return next((i for i in INTEGRATIONS if i.name == name), None)


def cloud_integrations() -> tuple[Integration, ...]:
    """The ones `/login` can report on."""
    return tuple(i for i in INTEGRATIONS if i.is_cloud)


def available_integrations() -> tuple[Integration, ...]:
    return tuple(i for i in INTEGRATIONS if i.available)


def missing_integrations() -> tuple[Integration, ...]:
    return tuple(i for i in INTEGRATIONS if not i.available)
