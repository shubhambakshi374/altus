"""Talking to the servers.

The ``mcp`` SDK is imported inside functions, not at module scope, so the whole
classifier keeps working with the extra uninstalled --- the same rule
``cloud/aws.py`` and ``cloud/gcp.py`` follow for their SDKs.

**Connections are per call, deliberately.** An MCP session is an async context
manager wrapping a task group, and holding one open across the life of a TUI
session means entering it in one task and exiting it in another, which is how
you get cancel-scope errors that surface as unrelated tool failures much later.
A fresh connection costs one round trip; the tool inventory, which is what gets
asked for repeatedly, is cached on the provider and costs nothing. When there
is a live server to measure against, that trade is worth revisiting --- until
then, correctness wins over a saved handshake.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from altus.mcp.catalog import Auth, ServerSpec, Transport, server_spec
from altus.mcp.classify import ToolInfo

log = logging.getLogger(__name__)

KEYRING_SERVICE = "altus"

CLIENT_NAME = "altus"
CLIENT_VERSION = "0.1.0"

OAUTH_REDIRECT = "http://localhost:8765/callback"
"""Where the browser comes back to. Loopback only: an OAuth code arriving at
any other host would be a code handed to somebody else."""


class McpError(RuntimeError):
    """A server refused, timed out, or is not reachable from here."""


def explain(exc: BaseException) -> str:
    """The message a human can act on, dug out of the task group.

    Both transports run inside anyio task groups, so a plain 401 arrives as
    ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`` ---
    which says nothing at all about a bad token. Flatten to the leaves, which
    is where the real cause is.
    """
    leaves: list[str] = []

    def walk(item: BaseException) -> None:
        if isinstance(item, BaseExceptionGroup):
            for sub in item.exceptions:
                walk(sub)
        else:
            text = str(item).strip()
            leaves.append(f"{type(item).__name__}: {text}" if text else type(item).__name__)

    walk(exc)
    seen = list(dict.fromkeys(leaves))
    return "; ".join(seen) if seen else str(exc)


def credential(spec: ServerSpec, name: str) -> str | None:
    """One credential: environment first, then the keyring.

    Environment first for the reason ``config/secrets.py`` gives --- CI and
    headless runs have no keyring backend --- and never the config file.
    """
    found = os.environ.get(name)
    if found:
        return found
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, f"mcp:{spec.id}:{name}")
    except Exception as exc:  # keyring backends fail in many creative ways
        log.debug("keyring unavailable for mcp:%s: %s", spec.id, exc)
        return None


def credentials(spec: ServerSpec) -> dict[str, str]:
    """Every credential the server needs that we can actually find."""
    return {name: value for name in spec.env if (value := credential(spec, name))}


def missing_credentials(spec: ServerSpec) -> tuple[str, ...]:
    """What is still needed. Datadog wants two headers, not one token."""
    found = credentials(spec)
    if spec.auth is Auth.HEADERS:
        return tuple(name for name in spec.env if name not in found)
    return () if found else spec.env


@dataclass
class McpProvider:
    """Lazy, cached, and reset when anything about a server changes.

    The same shape as ``AzureProvider`` and ``GcpProvider``: it can call
    anything, and deciding what may be called is somebody else's job.
    """

    timeout: float = 30.0
    max_result_bytes: int = 100_000
    scopes: dict[str, str] = field(default_factory=dict)
    """Per-server endpoint scope --- the Databricks ``genie/<space>`` or
    ``functions/<catalog>/<schema>`` that decides what the endpoint can do."""
    urls: dict[str, str] = field(default_factory=dict)
    """Per-server URL overrides, for the two vendors whose endpoint contains
    the customer's own account or workspace."""
    _tools: dict[str, tuple[ToolInfo, ...]] = field(default_factory=dict, repr=False)

    def reset(self, server: str = "") -> None:
        """Drop cached inventories. A tool list belongs to a credential."""
        if server:
            self._tools.pop(server, None)
        else:
            self._tools.clear()

    def url_for(self, spec: ServerSpec) -> str:
        """The endpoint, with the customer's own parts filled in.

        A URL still carrying a placeholder is a configuration error, not
        something to send a request at and find out.
        """
        url = self.urls.get(spec.id) or spec.url
        scope = self.scopes.get(spec.id, "")
        if scope and "{scope}" in url:
            url = url.replace("{scope}", scope.strip("/"))
        if "{" in url:
            missing = url[url.index("{") + 1 : url.index("}")] if "}" in url else "?"
            raise McpError(
                f"{spec.id}: the endpoint needs {missing!r}. "
                f"Set it under [mcp.{spec.id}] url in config.toml."
            )
        return url

    async def tools(self, server: str) -> tuple[ToolInfo, ...]:
        """Everything the server offers, as it described itself."""
        cached = self._tools.get(server)
        if cached is not None:
            return cached
        try:
            async with self._session(server) as session:
                result = await session.list_tools()
        except McpError:
            raise
        except BaseException as exc:
            # The SDK reports an HTTP failure as a JSON-RPC internal error and
            # discards the status code, so a rejected token and a server outage
            # are literally the same string. Say what it usually is.
            raise McpError(
                f"{server}: {explain(exc)}. Listing tools requires working "
                f"credentials on most servers, so a rejected token looks like this."
            ) from exc
        found = tuple(ToolInfo.from_mcp(tool) for tool in result.tools)
        self._tools[server] = found
        return found

    async def tool(self, server: str, name: str) -> ToolInfo | None:
        return next((t for t in await self.tools(server) if t.name == name), None)

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> str:
        """Invoke one tool and return its text.

        Whether this tool *may* be invoked was decided before we got here.
        """
        try:
            async with self._session(server) as session:
                result = await session.call_tool(tool, args, read_timeout_seconds=self.timeout)
        except McpError:
            raise
        except BaseException as exc:
            raise McpError(f"{server}.{tool}: {explain(exc)}") from exc
        text = _text_of(result)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise McpError(f"{server}.{tool} failed: {text}")
        if len(text) > self.max_result_bytes:
            text = text[: self.max_result_bytes] + "\n\n[truncated by altus]"
        return text

    def _session(self, server: str) -> Any:
        spec = server_spec(server)
        if spec is None:
            raise McpError(f"{server!r} is not a server Altus ships")
        missing = missing_credentials(spec)
        if missing:
            raise McpError(f"{server}: no credentials --- {spec.missing_hint}")
        if spec.transport is Transport.STDIO:
            return _stdio_session(spec, credentials(spec))
        return _http_session(spec, self.url_for(spec), credentials(spec))


def _text_of(result: Any) -> str:
    """Flatten a CallToolResult to text.

    Structured content wins when a server returns it, because it is the thing
    that survives round-tripping; otherwise the text blocks, joined.
    """
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured is not None:
        import json

        return json.dumps(structured, indent=2, default=str)
    parts: list[str] = []
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    return "\n".join(parts)


def _client_info() -> Any:
    from mcp.types import Implementation

    return Implementation(name=CLIENT_NAME, version=CLIENT_VERSION)


@asynccontextmanager
async def _open(transport: Any) -> AsyncIterator[Any]:
    """An open connection, for the length of one call.

    This has to be a single ``async with`` chain, and an earlier version that
    drove ``__aenter__``/``__aexit__`` by hand was wrong. Both transports wrap
    an anyio task group, and a task group must be exited by the task that
    entered it. Hand-driving the generator meant that on the *error* path ---
    a bad token, a server that will not start --- cleanup was finalized by the
    garbage collector instead, in another task, and anyio raised
    ``RuntimeError: Attempted to exit cancel scope in a different task`` on top
    of whatever had actually gone wrong. Reproducible on both transports, and
    it buried the real error under a traceback about cancel scopes.

    Letting ``async with`` drive it keeps entry and exit in the caller's task,
    which is the whole requirement.
    """
    from mcp import ClientSession

    async with transport as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write, client_info=_client_info()) as session:
            await session.initialize()
            yield session


def _stdio_params(spec: ServerSpec, creds: dict[str, str]) -> Any:
    """How the subprocess is launched, and what it is allowed to see.

    Only this server's own credentials plus PATH. Handing a subprocess
    ``os.environ`` would pass a third-party binary every other credential on
    the machine --- the cloud keys, the model API keys, all of it.
    """
    from mcp import StdioServerParameters

    command, *args = spec.command
    return StdioServerParameters(
        command=command,
        args=list(args),
        env={"PATH": os.environ.get("PATH", ""), **creds},
    )


def _stdio_session(spec: ServerSpec, creds: dict[str, str]) -> Any:
    from mcp.client.stdio import stdio_client

    return _open(stdio_client(_stdio_params(spec, creds)))


def _http_session(spec: ServerSpec, url: str, creds: dict[str, str]) -> Any:
    from mcp.client.streamable_http import streamable_http_client

    return _open(streamable_http_client(url, http_client=_http_client(spec, url, creds)))


def _http_client(spec: ServerSpec, url: str, creds: dict[str, str]) -> Any:
    import httpx

    if spec.auth is Auth.HEADERS:
        return httpx.AsyncClient(headers=creds, timeout=None)
    if spec.auth is Auth.TOKEN:
        token = next(iter(creds.values()))
        return httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=None)
    return httpx.AsyncClient(auth=_oauth(spec, url), timeout=None)


def _oauth(spec: ServerSpec, url: str) -> Any:
    """OAuth 2.1 with dynamic client registration, tokens kept in the keyring.

    Never on disk: a refresh token in a dotfile is a standing grant to the
    user's Jira, and the keyring is where every other Altus credential lives.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name=CLIENT_NAME,
            redirect_uris=[AnyUrl(OAUTH_REDIRECT)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=KeyringTokens(spec.id),
    )


@dataclass
class KeyringTokens:
    """``TokenStorage`` backed by the OS keyring."""

    server: str

    def _get(self, kind: str) -> str | None:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, f"mcp:{self.server}:{kind}")
        except Exception as exc:
            log.debug("keyring unavailable for mcp:%s:%s: %s", self.server, kind, exc)
            return None

    def _set(self, kind: str, value: str) -> None:
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, f"mcp:{self.server}:{kind}", value)
        except Exception as exc:
            # A token we cannot store means re-authenticating next time, which
            # is annoying. Failing the call instead would be worse.
            log.warning("could not store the %s token for %s: %s", kind, self.server, exc)

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        raw = self._get("tokens")
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: Any) -> None:
        self._set("tokens", tokens.model_dump_json())

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._get("client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, info: Any) -> None:
        self._set("client", info.model_dump_json())
