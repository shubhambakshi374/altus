"""Configuration schema. Secrets are never represented here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderSettings(BaseModel):
    """Non-secret, per-provider connection settings."""

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_version: str | None = None
    """Azure AI Foundry only."""
    region: str | None = None
    """AWS Bedrock only."""
    aws_profile: str | None = None
    """AWS Bedrock only; names a profile in the standard boto3 credential chain."""
    timeout: float = 600.0
    max_retries: int = 4
    extra_headers: dict[str, str] = Field(default_factory=dict)


class WorkspaceSettings(BaseModel):
    """The filesystem context tools operate in. See altus/workspace.py."""

    model_config = ConfigDict(extra="forbid")

    extra_roots: list[str] = Field(default_factory=list)
    """Opt-in paths outside the working directory, e.g. /etc/nginx."""
    deny_secrets: bool = True
    """Block credential-shaped files even inside an allowed root."""


class ToolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_iterations: int = 25
    max_file_bytes: int = 262_144
    max_output_bytes: int = 102_400
    """Total tool-result bytes per loop iteration."""


class Profile(BaseModel):
    """A named provider + model + sampling combination."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    max_tokens: int = 4096
    temperature: float | None = None
    system: str | None = None
    base_url: str = ""
    """For self-hosted endpoints. Required by the `local` provider."""
    api_key_env: str = ""
    """Environment variable holding the key, for endpoints that need one."""
    supports_tools: bool | None = None
    """Override capability detection. None means ask the server, or assume yes."""


class ProtectedSettings(BaseModel):
    """Targets too important to change on a single keypress."""

    model_config = ConfigDict(extra="forbid")

    patterns: list[str] = Field(default_factory=lambda: ["*prod*", "*production*"])
    """Matched case-insensitively against context, cluster, region and namespace."""
    accounts: list[str] = Field(default_factory=list)
    mode: Literal["confirm", "deny"] = "confirm"


class K8sSettings(BaseModel):
    """Which classes of Kubernetes capability this machine offers at all.

    A `false` here means the tool is never registered, so the model is not told
    it exists. That is deliberately stronger than refusing at call time: a tool
    the model cannot see costs no context and cannot be argued into being used.
    """

    model_config = ConfigDict(extra="forbid")

    allow_exec: bool = True
    """exec, attach and cp. Running a command inside a container is the single
    largest escalation here: whatever the container can reach, so can a chat
    message."""
    allow_port_forward: bool = True
    """Opens a tunnel from this machine into the cluster network."""
    allow_node_lifecycle: bool = True
    """cordon, uncordon, taint, drain."""
    allow_rbac_writes: bool = True
    """Creating or changing Roles, Bindings, ServiceAccounts and CSRs."""
    allow_cli: bool = True
    """kubectl, helm and kustomize, when the native tools cannot express it."""
    exec_timeout: int = 60
    """Seconds before an exec is cut off and what it printed so far returned."""


class AwsSettings(BaseModel):
    """Which classes of AWS capability this machine offers.

    Unlike the Kubernetes switches, most of these cannot work by withholding a
    tool: the same ``aws_write`` tags a volume and rewrites a trust policy. They
    are checked at the approval gate instead.
    """

    model_config = ConfigDict(extra="forbid")

    allow_writes: bool = True
    """Any mutating call at all."""
    allow_iam_writes: bool = True
    """Writes to IAM, STS, Organizations, KMS and the other identity services."""
    allow_delete: bool = True
    """Terminate, delete, destroy --- the irreversible verbs."""
    allow_cost_explorer: bool = True
    """Cost Explorer bills per request, so it can be switched off entirely."""
    max_results: int = 500
    """Results returned from one call. Paginators will happily walk a hundred
    thousand objects, and the model pays for every one of them."""


class AzureSettings(BaseModel):
    """Which classes of Azure capability this machine offers.

    Like the AWS switches and unlike the Kubernetes ones, most of these cannot
    work by withholding a tool: the same ``azure_write`` sets a tag and a role
    assignment. They are checked at the approval gate instead.

    There is no ``allow_cost_explorer`` here on purpose. AWS needed one because
    Cost Explorer bills about a cent a request; Azure's Cost Management query
    API is free, so the switch would protect against nothing.
    """

    model_config = ConfigDict(extra="forbid")

    allow_writes: bool = True
    """Any mutating call at all."""
    allow_rbac_writes: bool = True
    """Writes to Microsoft.Authorization, ManagedIdentity, AAD and Key Vault ---
    role assignments, policy, and resource locks. Removing a lock is how you get
    around a lock, so it belongs in the same class as what it protects."""
    allow_delete: bool = True
    """The irreversible verb."""
    allow_cli: bool = True
    """The `az` fallback, when the native azure_* tools cannot express it."""
    max_results: int = 500
    """Rows returned from one call. ARM will page through a whole subscription
    given the chance, and the model pays for every row."""


class GcpSettings(BaseModel):
    """Which classes of GCP capability this machine offers.

    Like the AWS and Azure switches, most of these cannot work by withholding a
    tool: the same ``gcp_write`` sets a label and a bucket's IAM policy. They
    are checked at the approval gate instead.
    """

    model_config = ConfigDict(extra="forbid")

    allow_writes: bool = True
    """Any mutating call at all."""
    allow_iam_writes: bool = True
    """setIamPolicy, service-account keys, KMS, Resource Manager --- the calls
    that decide who may do what. setIamPolicy alone is how a bucket becomes
    world-readable and how anyone grants themselves owner."""
    allow_delete: bool = True
    """The destructive verbs."""
    allow_cli: bool = True
    """The `gcloud` fallback, when the native gcp_* tools cannot express it."""
    billing_export_table: str = ""
    """`project.dataset.gcp_billing_export_v1_XXXXXX`.

    GCP has no spend API --- Cloud Billing exposes account metadata and SKU
    pricing and not a cent of actual cost --- so real spend lives only in a
    BigQuery export you configure yourself. Empty means gcp_cost says so and
    falls back to listing budgets rather than inventing a number."""
    max_results: int = 500
    """Rows returned from one call. Paginators will walk a whole project, and
    the model pays for every row."""


class McpServerSettings(BaseModel):
    """Per-server settings for one of the shipped MCP servers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    """None means autodetect: offer it when its credentials are present."""
    url: str = ""
    """Overrides the catalogued endpoint. Required for the two vendors whose
    URL contains the customer's own account or workspace."""
    scope: str = ""
    """The endpoint path that decides what it can do --- Databricks
    `genie/<space>`, `vector-search/<catalog>/<schema>`, `functions/...`."""
    toolsets: list[str] = Field(default_factory=list)
    """Narrows what the server offers at all, where it supports that. Fewer
    tools is less to classify and less that can drift."""


class WorkflowSettings(BaseModel):
    """Workflows: where they live, and who may write one.

    ``allow_model_authoring`` is not a convenience switch. A workflow file is a
    queued set of actions against real infrastructure, so letting the model
    write one is a different permission from letting it call a tool, and
    somebody running Altus in anger should be able to say workflows come only
    from humans.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    dir: str = ""
    """Empty means <config>/workflows."""
    allow_model_authoring: bool = True


class McpCustomSettings(BaseModel):
    """A server Altus has never heard of, configured by the user.

    The catalogue can never be finished --- Darktrace has no official MCP
    server today --- and the alternative to a door is that somebody forks
    Altus to add one. What this does not do is pretend: a custom server has no
    manifest, so every tool it publishes fails closed to a typed challenge
    unless the server itself declares it read-only, and every surface that
    lists it says so.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    summary: str = ""
    url: str = ""
    """For an HTTP server. Leave empty and set `command` for a local one."""
    command: list[str] = Field(default_factory=list)
    """argv for a stdio server. argv[0] must be on PATH."""
    auth: Literal["token", "oauth", "headers"] = "token"
    env: list[str] = Field(default_factory=list)
    """Environment variables carrying credentials, most significant first.
    Also how Altus decides the server is worth offering at all."""
    scope: str = ""
    reference: str = ""


class McpSettings(BaseModel):
    """The MCP servers Altus ships kitted out.

    Deliberately not a place to name arbitrary servers. Altus classifies tools
    against a curated manifest, and a server with no manifest would have every
    tool fail closed to privileged --- a challenge on every call, which is how
    a challenge stops being read.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    servers: list[str] = Field(default_factory=list)
    """Empty means every server whose credentials are present."""
    allow_writes: bool = True
    """Any mutating call at all. Turning this off also passes the servers'
    own read-only switches --- GitHub's GITHUB_READ_ONLY and Grafana's
    --disable-write --- so Altus is not the only thing enforcing it."""
    timeout: float = 30.0
    max_rows: int = 200
    """Query result rows. Snowflake, Databricks and the SQL-shaped tools
    return data rather than metadata, and every row reaches the model."""
    max_result_bytes: int = 100_000
    github: McpServerSettings = Field(default_factory=McpServerSettings)
    atlassian: McpServerSettings = Field(default_factory=McpServerSettings)
    crowdstrike: McpServerSettings = Field(default_factory=McpServerSettings)
    servicenow: McpServerSettings = Field(default_factory=McpServerSettings)
    grafana: McpServerSettings = Field(default_factory=McpServerSettings)
    datadog: McpServerSettings = Field(default_factory=McpServerSettings)
    newrelic: McpServerSettings = Field(default_factory=McpServerSettings)
    snowflake: McpServerSettings = Field(default_factory=McpServerSettings)
    databricks: McpServerSettings = Field(default_factory=McpServerSettings)

    custom: dict[str, McpCustomSettings] = Field(default_factory=dict)
    """`[mcp.custom.<name>]` --- servers Altus does not ship. Unclassified."""

    def for_server(self, server: str) -> McpServerSettings:
        found = getattr(self, server, None)
        if isinstance(found, McpServerSettings):
            return found
        entry = self.custom.get(server)
        if entry is not None:
            # One door onto per-server settings, so a custom server's url and
            # scope reach the provider by the same route a shipped one's do.
            return McpServerSettings(enabled=entry.enabled, url=entry.url, scope=entry.scope)
        return McpServerSettings()

    @property
    def server_ids(self) -> list[str]:
        """Every server this config could offer, shipped or custom."""
        from altus.mcp.catalog import CATALOG

        return [spec.id for spec in CATALOG] + sorted(self.custom)


class CloudSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_redaction: bool = True
    """Scrub secret material from tool output before it reaches the model.

    Tool results are transmitted to the active LLM provider, so turning this
    off means Kubernetes Secrets and AWS session tokens leave your machine.
    """
    kubeconfigs: list[str] = Field(default_factory=list)
    """Extra kubeconfig files, added with /kube add."""
    kube_context: str | None = None
    """The context Altus uses."""
    kube_context_scope: Literal["altus", "global"] = "altus"
    """`altus` keeps the selection to this tool. `global` also writes
    current-context to your kubeconfig, like `kubectl config use-context` ---
    which retargets every other terminal you have open, so it is opt-in."""

    @field_validator("kube_context_scope", mode="before")
    @classmethod
    def _accept_the_old_spelling(cls, value: object) -> object:
        """This value lives in people's config files, and the rename changed it.

        Without this, an existing config.toml saying `kube_context_scope =
        "wai"` stops loading altogether --- a validation error on a key nobody
        touched, at startup, with no obvious cause.
        """
        return "altus" if value == "wai" else value

    default_region: str | None = None
    gcp_project: str | None = None
    """The GCP project Altus acts in. ADC often sees many, and every tool acts
    in exactly one --- so that the blast radius named in a prompt is the one
    that is actually touched."""
    azure_subscription: str | None = None
    """The Azure subscription Altus acts in. One credential commonly sees many,
    and every tool acts in exactly one --- so that the blast radius named in a
    prompt is the one that is actually touched."""
    dry_run_first: bool = True
    cli_fallback: bool = True
    cli_allowlist: list[str] = Field(
        default_factory=lambda: ["kubectl", "aws", "az", "gcloud", "helm", "terraform"]
    )
    protected: ProtectedSettings = Field(default_factory=ProtectedSettings)
    k8s: K8sSettings = Field(default_factory=K8sSettings)
    aws: AwsSettings = Field(default_factory=AwsSettings)
    azure: AzureSettings = Field(default_factory=AzureSettings)
    gcp: GcpSettings = Field(default_factory=GcpSettings)


class UISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: str = "textual-dark"
    stream_flush_ms: int = 50
    """How often streamed text is flushed into the transcript widget."""
    show_reasoning: bool = True
    graphics: Literal["auto", "image", "cells", "off"] = "auto"
    """How visuals are drawn.

    `auto` follows the terminal: images where it speaks Kitty's protocol or
    Sixel, box-drawing characters everywhere else. The explicit values are a
    ceiling rather than a floor --- asking for images on a terminal that cannot
    show them still yields cells, because the alternative is a broken screen.
    """
    graphics_font: str = ""
    """Absolute path to a TTF for drawn labels. Empty means discover one."""


DEFAULT_PROFILE_NAME = "default"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_profile: str = DEFAULT_PROFILE_NAME
    profiles: dict[str, Profile] = Field(
        default_factory=lambda: {
            DEFAULT_PROFILE_NAME: Profile(
                provider="anthropic", model="claude-sonnet-5", max_tokens=8192
            )
        }
    )
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    ui: UISettings = Field(default_factory=UISettings)

    def provider_settings(self, name: str) -> ProviderSettings:
        return self.providers.get(name, ProviderSettings())
