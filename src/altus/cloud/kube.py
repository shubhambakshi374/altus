"""Kubeconfig discovery and context selection.

**By default nothing here writes to the user's kubeconfig.** Listing contexts
reads the file; selecting one records the choice in Altus's own state, because
changing the global context as a side effect of a chat message would silently
retarget every other terminal the user has open.

``set_current_context`` is the opt-in exception, reached only when
``[cloud] kube_context_scope = "global"`` or an explicit ``--global``.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from altus.cloud.base import CloudTarget, Sensitivity

DEFAULT_KUBECONFIG = Path.home() / ".kube" / "config"

#: Verbs the Kubernetes API itself defines. Classification is exact here ---
#: unlike AWS there is no guessing from a name.
READ_VERBS = frozenset({"get", "list", "watch"})
MUTATE_VERBS = frozenset(
    {
        "create",
        "update",
        "patch",
        "replace",
        "delete",
        "deletecollection",
        "apply",
        "scale",
        "rollout",
    }
)

#: Kinds whose payload is credential material whatever the verb.
SECRET_KINDS = frozenset({"Secret", "ServiceAccount", "CertificateSigningRequest"})

#: Subresources that are a different operation from their parent. ``pods/exec``
#: is reached with the verb ``get``, and it runs a process --- so the verb is
#: worthless here and the subresource decides on its own.
PRIVILEGED_SUBRESOURCES = frozenset(
    {
        "exec",  # runs a process in a container
        "attach",  # joins one already running
        "portforward",  # opens a tunnel into the cluster network
        "proxy",  # same, through the API server
        "token",  # mints a credential that outlives this session
        "approval",  # signs a certificate
        "binding",  # assigns a pod to a node directly
        "eviction",  # removes a running pod
        "escalate",  # grants rights the grantor does not hold
        "impersonate",  # acts as someone else
    }
)

#: Kinds where *changing* one rewrites who may do what, or takes capacity out
#: of service. Reading them is ordinary; writing them is not.
PRIVILEGED_KINDS = frozenset(
    {
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
        "ServiceAccount",
        "CertificateSigningRequest",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "Node",
        "PodSecurityPolicy",
        "APIService",
        "CustomResourceDefinition",
    }
)


def classify(verb: str, kind: str = "", subresource: str = "") -> Sensitivity:
    """Where an operation sits on the four-level scale.

    Precedence runs highest-first and the subresource outranks the verb,
    because that is the direction the danger actually flows.
    """
    verb = verb.casefold()
    subresource = subresource.casefold().lstrip("/")

    if subresource in PRIVILEGED_SUBRESOURCES:
        return Sensitivity.PRIVILEGED
    if verb in MUTATE_VERBS and kind in PRIVILEGED_KINDS:
        return Sensitivity.PRIVILEGED
    if verb == "deletecollection":
        # One call, an unbounded number of objects, and no per-object prompt.
        return Sensitivity.PRIVILEGED
    if verb in MUTATE_VERBS:
        return Sensitivity.MUTATE
    if verb in READ_VERBS:
        return Sensitivity.SENSITIVE_READ if kind in SECRET_KINDS else Sensitivity.READ
    # An unknown verb fails closed to the strictest level, not the
    # second-strictest: a verb we have never seen is exactly the case where
    # guessing low is unrecoverable. (An unrecognised *subresource* is not this
    # case --- it falls through to its verb above, so `get deployments/status`
    # stays an ordinary read.)
    return Sensitivity.PRIVILEGED


@dataclass(frozen=True)
class KubeContext:
    name: str
    cluster: str = ""
    user: str = ""
    namespace: str = "default"
    source: str = ""
    """Which kubeconfig file it came from."""

    def target(self, namespace: str | None = None) -> CloudTarget:
        """``None`` means "this context's namespace"; ``""`` means cluster-scoped.

        The distinction matters: a Node operation has no namespace, and folding
        that into "default" tested the protection rules against a namespace the
        operation was never touching.
        """
        return CloudTarget(
            cloud="k8s",
            context=self.name,
            location=self.cluster,
            scope=self.namespace if namespace is None else namespace,
        )


def kubeconfig_paths(extra: tuple[str, ...] = ()) -> list[Path]:
    """Every kubeconfig to consider: KUBECONFIG, the default, plus registered ones."""
    paths: list[Path] = []
    env = os.environ.get("KUBECONFIG")
    if env:
        paths.extend(Path(p).expanduser() for p in env.split(os.pathsep) if p)
    elif DEFAULT_KUBECONFIG.exists():
        paths.append(DEFAULT_KUBECONFIG)
    paths.extend(Path(p).expanduser() for p in extra)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            unique.append(resolved)
    return unique


def list_contexts(extra: tuple[str, ...] = ()) -> tuple[list[KubeContext], str | None]:
    """Every context across every kubeconfig, plus the file's own active one.

    Read-only: opens the files and closes them.
    """
    from kubernetes import config as kube_config

    found: list[KubeContext] = []
    active: str | None = None
    for path in kubeconfig_paths(extra):
        try:
            # kubernetes 37 added a py.typed marker without annotating its
            # functions, so --strict now sees a typed package full of untyped
            # defs. Ignored at the three call sites rather than by relaxing
            # disallow_untyped_calls for this module: the override would apply
            # to everything in here, including our own code.
            contexts, current = kube_config.list_kube_config_contexts(  # type: ignore[no-untyped-call]
                config_file=str(path)
            )
        except Exception:  # unreadable or malformed; skip rather than fail the command
            continue
        if active is None and current:
            active = current.get("name")
        for entry in contexts or []:
            detail = entry.get("context", {})
            found.append(
                KubeContext(
                    name=entry.get("name", ""),
                    cluster=detail.get("cluster", ""),
                    user=detail.get("user", ""),
                    namespace=detail.get("namespace") or "default",
                    source=str(path),
                )
            )
    return found, active


def resolve_context(name: str | None, extra: tuple[str, ...] = ()) -> KubeContext | None:
    """The context Altus should use: the selected one, else the kubeconfig's own."""
    contexts, active = list_contexts(extra)
    if not contexts:
        return None
    wanted = name or active
    return next((c for c in contexts if c.name == wanted), None) or (
        contexts[0] if name is None else None
    )


def build_client(context: str | None, extra: tuple[str, ...] = ()) -> Any:
    """A dynamic client bound to one context, without touching the kubeconfig.

    ``new_client_from_config`` takes the context by name and returns a client
    configured for it; the file on disk is never rewritten.
    """
    from kubernetes import client as kube_client
    from kubernetes import config as kube_config
    from kubernetes.dynamic import DynamicClient

    paths = kubeconfig_paths(extra)
    config_file = str(paths[0]) if paths else None
    # Untyped despite kubernetes 37's py.typed marker; see list_contexts.
    api = kube_config.new_client_from_config(  # type: ignore[no-untyped-call]
        config_file=config_file, context=context
    )
    return DynamicClient(  # type: ignore[no-untyped-call]
        kube_client.ApiClient(configuration=api.configuration)
    )


def set_current_context(name: str, extra: tuple[str, ...] = ()) -> Path:
    """Write `current-context` into the kubeconfig, preserving the rest.

    Only ever called when the user opted into global scope. Atomic: a crash
    mid-write must not leave someone without a usable kubeconfig.
    """
    import os
    import tempfile

    import yaml

    paths = kubeconfig_paths(extra)
    if not paths:
        raise FileNotFoundError("no kubeconfig to write to")
    target = paths[0]
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not any(c.get("name") == name for c in document.get("contexts") or []):
        raise KeyError(f"no context named {name!r} in {target}")
    document["current-context"] = name

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".kubeconfig.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False)
        os.chmod(tmp, target.stat().st_mode & 0o777)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target
