"""The servers Altus ships ready to use.

The user does not bring a config file. Altus knows these seven, knows how each
one authenticates, and knows which of them are even worth trying on this
machine --- that is what ``detect`` is for.

Three of them behave quite differently from the others, and the difference
drives the whole classifier:

* **Fixed** (GitHub, Grafana, Datadog, New Relic) publish a stable tool list, so
  a manifest of names works and anything absent from it is drift.
* **Scoped** (Atlassian) does not publish every name, but its names are
  prefixed by permission scope --- ``read_jira`` covers ``getJiraIssue``,
  ``write_jira`` covers ``createJiraIssue``.
* **Deployment-defined** (Snowflake, Databricks) let whoever created the server
  object choose the tool names. A name manifest is *structurally impossible*
  there: a Unity Catalog functions endpoint exposes one tool per UDF, and those
  are the customer's own functions.

For the third class, failing closed on every name would mean a typed challenge
on every call, which is the failure mode that teaches people to type through
challenges. So those entries declare ``unknown`` from what the *endpoint* is
structurally capable of, with the reason recorded in ``unknown_why``. That is
the same argument that lets ``azure_get`` be read-only because it only ever
issues GET --- the capability is a property of the door, not of the label.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from altus.cloud.base import Sensitivity


class Transport(StrEnum):
    HTTP = "http"
    """Streamable HTTP against the vendor's hosted endpoint."""
    STDIO = "stdio"
    """A local subprocess speaking MCP over stdin/stdout."""


class Auth(StrEnum):
    OAUTH = "oauth"
    """OAuth 2.1, browser flow, token held in the keyring."""
    TOKEN = "token"
    """A bearer token or API key, from the environment or the keyring."""
    HEADERS = "headers"
    """Two or more named headers, as Datadog wants."""


@dataclass(frozen=True)
class ServerSpec:
    """One shipped server: what it covers, how to reach it, how to harden it."""

    id: str
    products: tuple[str, ...]
    """What a user would call it. Atlassian is one server and five products."""
    summary: str
    transport: Transport
    auth: Auth
    url: str = ""
    """For HTTP. May contain ``{}`` placeholders filled from settings."""
    command: tuple[str, ...] = ()
    """For STDIO. argv[0] must exist on PATH or the server is unavailable."""
    env: tuple[str, ...] = ()
    """Environment variables carrying credentials, most significant first."""
    read_only_flag: tuple[str, ...] = ()
    """What to pass so the *server itself* refuses writes. We are not the only
    thing that should stand between the model and a force-push."""
    unknown: Sensitivity = Sensitivity.PRIVILEGED
    """How a tool absent from the manifest classifies."""
    unknown_why: str = "this tool is not in Altus's manifest"
    reference: str = ""
    notes: str = ""
    _detect: Callable[[], bool] | None = field(default=None, repr=False)

    def available(self) -> bool:
        """Whether this is worth offering --- the user's "if present".

        Credentials in the environment, or a binary on PATH. Never a network
        call: this runs on every ``/mcp`` and must not hang.
        """
        if self._detect is not None:
            return self._detect()
        if self.transport is Transport.STDIO and self.command and _missing(self.command[0]):
            return False
        return any(os.environ.get(name) for name in self.env) if self.env else False

    @property
    def missing_hint(self) -> str:
        """Why it is unavailable, in the terms the user can act on."""
        if self.transport is Transport.STDIO and self.command and _missing(self.command[0]):
            return f"{self.command[0]} is not on PATH"
        if self.env:
            return f"set {' or '.join(self.env)}"
        return "no credentials found"


def _missing(binary: str) -> bool:
    return shutil.which(binary) is None


def _databricks_detect() -> bool:
    return bool(os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN"))


CATALOG: tuple[ServerSpec, ...] = (
    ServerSpec(
        id="github",
        products=("GitHub",),
        summary="Repositories, issues, pull requests, Actions and code scanning",
        transport=Transport.HTTP,
        auth=Auth.TOKEN,
        url="https://api.githubcopilot.com/mcp/",
        env=("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"),
        read_only_flag=("GITHUB_READ_ONLY=true",),
        reference="https://github.com/github/github-mcp-server",
    ),
    ServerSpec(
        id="atlassian",
        products=("Jira", "Confluence", "Bitbucket Cloud", "Jira Service Management", "Compass"),
        summary="Issues, pages, repositories and service requests",
        transport=Transport.HTTP,
        auth=Auth.OAUTH,
        url="https://mcp.atlassian.com/v2/mcp",
        env=("ATLASSIAN_API_TOKEN",),
        reference="https://atlassian.github.io/atlassian-mcp-server/",
        notes=(
            "Cloud only. Jira Data Center and Jira Server cannot connect to this "
            "server at all, and Atlassian has published no timeline for that changing."
        ),
    ),
    ServerSpec(
        id="grafana",
        products=("Grafana", "Loki", "Prometheus", "Tempo", "Pyroscope", "OnCall"),
        summary="Dashboards, datasource queries, incidents, alerting and on-call",
        transport=Transport.STDIO,
        auth=Auth.TOKEN,
        command=("mcp-grafana",),
        env=("GRAFANA_API_KEY", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
        read_only_flag=("--disable-write",),
        reference="https://github.com/grafana/mcp-grafana",
    ),
    ServerSpec(
        id="datadog",
        products=("Datadog",),
        summary="Metrics, logs, traces, monitors, incidents and security signals",
        transport=Transport.HTTP,
        auth=Auth.HEADERS,
        url="https://mcp.datadoghq.com/api/unstable/mcp-server",
        env=("DD_API_KEY", "DD_APPLICATION_KEY"),
        reference="https://docs.datadoghq.com/mcp_server/tools/",
        notes="Scope the application key to read-only unless writes are actually wanted.",
    ),
    ServerSpec(
        id="newrelic",
        products=("New Relic",),
        summary="Entities, NRQL, alerts, errors and deployment impact",
        transport=Transport.STDIO,
        auth=Auth.TOKEN,
        command=("npx", "-y", "@newrelic/mcp-server"),
        env=("NEW_RELIC_API_KEY",),
        reference="https://docs.newrelic.com/docs/agentic-ai/mcp/tool-reference/",
        notes="Every documented tool is a read; there is no write path to gate.",
    ),
    ServerSpec(
        id="snowflake",
        products=("Snowflake",),
        summary="Cortex Analyst and Search, and whatever SQL the server object allows",
        transport=Transport.HTTP,
        auth=Auth.OAUTH,
        url="https://{account}/api/v2/databases/{database}/schemas/{schema}/mcp-servers/{name}",
        env=("SNOWFLAKE_ACCOUNT",),
        unknown=Sensitivity.PRIVILEGED,
        unknown_why=(
            "Snowflake MCP tool names are chosen by whoever created the server object, so "
            "a name Altus does not know could be a Cortex search or SYSTEM_EXECUTE_SQL"
        ),
        reference="https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp",
        notes=(
            "Tools are declared per deployment with a type: CORTEX_ANALYST_MESSAGE, "
            "CORTEX_SEARCH_SERVICE_QUERY, CORTEX_AGENT_RUN, SYSTEM_EXECUTE_SQL or GENERIC. "
            "SYSTEM_EXECUTE_SQL takes an optional read-only restriction that no client "
            "can verify, so Altus treats it as arbitrary execution regardless."
        ),
    ),
    ServerSpec(
        id="databricks",
        products=("Databricks",),
        summary="Genie spaces, Vector Search indexes and Unity Catalog functions",
        transport=Transport.HTTP,
        auth=Auth.TOKEN,
        url="https://{workspace}/api/2.0/mcp/{scope}",
        env=("DATABRICKS_TOKEN",),
        unknown=Sensitivity.PRIVILEGED,
        unknown_why=(
            "a Databricks tool name Altus does not know is a Unity Catalog function, and "
            "a UDF can do anything its owner wrote it to do"
        ),
        reference="https://docs.databricks.com/aws/en/generative-ai/mcp/managed-mcp",
        notes=(
            "Three endpoint shapes with different capability: genie/<space> exposes exactly "
            "query_space and poll_response; vector-search/<catalog>/<schema> exposes one "
            "index query per index and can do nothing else; functions/<catalog>/<schema> "
            "exposes arbitrary SQL and Python UDFs."
        ),
        _detect=_databricks_detect,
    ),
)

#: Endpoint-shape overrides for Databricks, where capability is a property of
#: the URL rather than of the tool name. ``vector-search`` cannot mutate
#: anything no matter what the index is called; ``functions`` runs whatever a
#: UDF author wrote.
DATABRICKS_SCOPES: dict[str, tuple[Sensitivity, str]] = {
    "genie": (Sensitivity.READ, "a Genie space answers questions and cannot write"),
    "vector-search": (
        Sensitivity.READ,
        "a Vector Search endpoint exposes one index query per index and can do nothing else",
    ),
    "functions": (
        Sensitivity.PRIVILEGED,
        "a Unity Catalog functions endpoint runs arbitrary SQL and Python UDFs",
    ),
}

_BY_ID = {spec.id: spec for spec in CATALOG}


def server_spec(server: str) -> ServerSpec | None:
    return _BY_ID.get(server)


def available_servers() -> tuple[ServerSpec, ...]:
    return tuple(spec for spec in CATALOG if spec.available())
