"""Cloud credential discovery and interactive login.

``status`` is deliberately **local only** --- it inspects credential files,
environment variables and caches, and makes no network call. Showing an auth
panel should never cost a round trip to four clouds, and it must work offline.

``verify`` is the opt-in network check ("who am I?"), and ``login`` is the
interactive flow.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

LOGIN_TIMEOUT = 300.0


@dataclass(frozen=True)
class CloudStatus:
    cloud: str
    available: bool
    """The SDK is installed."""
    authenticated: bool
    source: str = ""
    detail: str = ""
    hint: str = ""

    @property
    def state(self) -> str:
        if not self.available:
            return "not installed"
        return "ready" if self.authenticated else "no credentials"


def _missing(cloud: str) -> CloudStatus:
    from altus.cloud.base import integration

    entry = integration(cloud)
    return CloudStatus(
        cloud=cloud,
        available=False,
        authenticated=False,
        hint=entry.install_hint if entry else "",
    )


def status(cloud: str, *, extra_kubeconfigs: tuple[str, ...] = ()) -> CloudStatus:
    from altus.cloud.base import integration

    entry = integration(cloud)
    if entry is None or not entry.available:
        return _missing(cloud)
    return {
        "k8s": lambda: _k8s_status(extra_kubeconfigs),
        "aws": _aws_status,
        "azure": _azure_status,
        "gcp": _gcp_status,
    }[cloud]()


def all_status(extra_kubeconfigs: tuple[str, ...] = ()) -> list[CloudStatus]:
    from altus.cloud.base import cloud_integrations

    return [status(i.name, extra_kubeconfigs=extra_kubeconfigs) for i in cloud_integrations()]


# --------------------------------------------------------------------- per cloud


def _k8s_status(extra: tuple[str, ...]) -> CloudStatus:
    from altus.cloud.kube import list_contexts

    contexts, active = list_contexts(extra)
    if not contexts:
        return CloudStatus("k8s", True, False, hint="add a kubeconfig with /kube add <path>")
    return CloudStatus(
        "k8s",
        True,
        True,
        source="kubeconfig",
        detail=f"{len(contexts)} context(s), active: {active or contexts[0].name}",
    )


def _aws_status() -> CloudStatus:
    try:
        import botocore.session

        session = botocore.session.get_session()
        credentials = session.get_credentials()
    except Exception as exc:
        return CloudStatus("aws", True, False, detail=str(exc))
    if credentials is None:
        return CloudStatus(
            "aws", True, False, hint="aws sso login, or set AWS_PROFILE / AWS_ACCESS_KEY_ID"
        )
    profile = os.environ.get("AWS_PROFILE", "default")
    region = session.get_config_variable("region") or "no default region"
    return CloudStatus(
        "aws", True, True, source=credentials.method, detail=f"profile {profile} · {region}"
    )


def _azure_status() -> CloudStatus:
    """Local detection only --- asking DefaultAzureCredential for a token is a
    network call, and this runs every time the auth panel is opened."""
    if os.environ.get("AZURE_CLIENT_ID") and os.environ.get("AZURE_TENANT_ID"):
        return CloudStatus("azure", True, True, source="env", detail="service principal")
    if os.environ.get("MSI_ENDPOINT") or os.environ.get("IDENTITY_ENDPOINT"):
        return CloudStatus("azure", True, True, source="managed-identity")
    msal_cache = Path.home() / ".azure" / "msal_token_cache.json"
    if (Path.home() / ".azure").exists() and msal_cache.exists():
        return CloudStatus("azure", True, True, source="azure-cli-cache", detail="~/.azure")
    altus_cache = _azure_cache_path()
    if altus_cache.exists():
        return CloudStatus("azure", True, True, source="altus-device-code", detail=str(altus_cache))
    return CloudStatus("azure", True, False, hint="/login azure  (device code, no az needed)")


def _gcp_status() -> CloudStatus:
    key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key and Path(key).exists():
        return CloudStatus("gcp", True, True, source="service-account", detail=key)
    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc.exists():
        return CloudStatus("gcp", True, True, source="adc", detail=str(adc))
    if os.environ.get("GCE_METADATA_HOST"):
        return CloudStatus("gcp", True, True, source="metadata")
    hint = (
        "gcloud auth application-default login"
        if shutil.which("gcloud")
        else "install gcloud, or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key"
    )
    return CloudStatus("gcp", True, False, hint=hint)


def _azure_cache_path() -> Path:
    from altus.config.loader import data_dir

    return data_dir() / "azure_token_cache.json"


# ------------------------------------------------------------------------ verify


async def verify(cloud: str) -> str:
    """Ask the cloud who we are. Makes a network call; read-only."""
    if cloud == "aws":

        def _whoami() -> str:
            import boto3

            identity = boto3.client("sts").get_caller_identity()
            return f"account {identity['Account']} · {identity['Arn']}"

        return await asyncio.to_thread(_whoami)
    if cloud == "azure":
        from altus.cloud.azure import AzureProvider

        provider = AzureProvider()
        try:
            identity = await provider.whoami()
        finally:
            await provider.close()
        return f"tenant {identity['tenant']} · {identity['principal'] or identity['object_id']}"
    if cloud == "gcp":
        from altus.cloud.gcp import GcpProvider

        identity = await GcpProvider().whoami()
        return f"project {identity['project'] or '(none)'} · {identity['account'] or identity['source']}"
    if cloud == "k8s":
        from altus.cloud.kube import list_contexts

        contexts, active = list_contexts()
        return f"{len(contexts)} contexts, active {active}"
    raise NotImplementedError(f"verify is not implemented for {cloud}")


# ------------------------------------------------------------------------- login


async def login(cloud: str, *, profile: str | None = None) -> tuple[bool, str]:
    """Run the interactive flow. Returns (succeeded, message)."""
    if cloud == "aws":
        return await _login_aws(profile)
    if cloud == "azure":
        return await _login_azure()
    if cloud == "gcp":
        return await _login_gcp()
    if cloud == "k8s":
        return False, "Kubernetes uses your kubeconfig; see /kube"
    return False, f"unknown cloud {cloud!r}"


async def _run(*cmd: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=LOGIN_TIMEOUT)
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed"
    except (OSError, TimeoutError) as exc:
        return 1, str(exc)
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def _login_aws(profile: str | None) -> tuple[bool, str]:
    if not shutil.which("aws"):
        return False, (
            "the AWS CLI is not installed. Set AWS_PROFILE, AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY, or install it for SSO login."
        )
    cmd = ["aws", "sso", "login"]
    if profile:
        cmd += ["--profile", profile]
    code, output = await _run(*cmd)
    return code == 0, output or ("signed in" if code == 0 else "sso login failed")


async def _login_azure() -> tuple[bool, str]:
    """Device code via azure-identity --- deliberately does not need `az`."""
    try:
        from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions
    except ImportError:
        return False, "azure support is not installed: uv tool install 'altus[azure]'"

    prompts: list[str] = []

    def _prompt(verification_uri: str, user_code: str, _expires: object) -> None:
        prompts.append(f"Open {verification_uri} and enter code {user_code}")

    def _acquire() -> str:
        credential = DeviceCodeCredential(
            prompt_callback=_prompt,
            cache_persistence_options=TokenCachePersistenceOptions(name="altus"),
        )
        token = credential.get_token("https://management.azure.com/.default")
        return "signed in" if token.token else "no token returned"

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_acquire), timeout=LOGIN_TIMEOUT)
    except Exception as exc:
        return False, f"{'; '.join(prompts)}\n{exc}" if prompts else str(exc)
    return True, "\n".join([*prompts, result])


async def _login_gcp() -> tuple[bool, str]:
    if not shutil.which("gcloud"):
        return False, (
            "gcloud is not installed, and Google has no device-code flow that works "
            "without a registered client. Point GOOGLE_APPLICATION_CREDENTIALS at a "
            "service-account key, or install the gcloud CLI."
        )
    code, output = await _run("gcloud", "auth", "application-default", "login")
    return code == 0, output or "signed in"
