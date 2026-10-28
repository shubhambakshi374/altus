"""Mistral adapter.

Close to the OpenAI shape but a distinct SDK, so it does not subclass the
OpenAI base: the client, the stream envelope (``event.data``) and the error
types all differ.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar

from altus.core.errors import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
)
from altus.core.events import (
    MessageEnd,
    MessageStart,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageUpdate,
)
from altus.core.types import (
    ChatRequest,
    Message,
    ModelInfo,
    ReasoningBlock,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from altus.providers.base import BaseProvider, ProviderCapabilities

if TYPE_CHECKING:
    # `mistralai.client`, not `mistralai`: the top-level package is a
    # namespace package with no re-exports, so `from mistralai import Mistral`
    # is an ImportError --- and was at the version this adapter was written
    # against. The import is lazy and the suite drives a fake provider, so
    # nothing surfaced it until the SDK surfaces were checked by hand.
    from mistralai.client import Mistral

_FINISH_REASONS = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "model_length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "error": StopReason.ERROR,
}


class MistralProvider(BaseProvider):
    name: ClassVar[str] = "mistral"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        streaming=True,
        tools=True,
        parallel_tool_calls=True,
        reasoning=False,
        vision=True,
        system_as_field=False,
        requires_max_tokens=False,
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: Mistral | None = None

    def _get_client(self) -> Mistral:
        if self._client is None:
            from mistralai.client import Mistral

            from altus.config.secrets import require_api_key

            self._client = Mistral(
                api_key=self.api_key or require_api_key(self.name),
                server_url=self.settings.base_url,
            )
        return self._client

    async def list_models(self) -> list[ModelInfo]:
        from altus.providers.registry import catalog_for

        catalog = {m.id: m for m in catalog_for(self.name)}
        try:
            response = await self._get_client().models.list_async()
        except Exception as exc:
            raise self._map_error(exc) from exc
        models: list[ModelInfo] = []
        for item in response.data or []:
            # The SDK types this list as a union that includes
            # `UnknownModelListData` --- a card it could not classify, which
            # carries no id. Skipped rather than coerced: a model we cannot
            # name is one the picker could not select either, and inventing an
            # id would put an unusable entry in the list.
            identifier = str(getattr(item, "id", "") or "")
            if not identifier:
                continue
            models.append(
                catalog.get(identifier)
                or ModelInfo(
                    id=identifier,
                    provider=self.name,
                    context_window=getattr(item, "max_context_length", 0) or 0,
                )
            )
        return models

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        client = self._get_client()
        body: dict[str, Any] = {
            "model": request.model,
            "messages": _to_mistral_messages(request),
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop_sequences:
            body["stop"] = request.stop_sequences
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        body.update(request.extra)

        try:
            raw_stream = await client.chat.stream_async(**body)
        except Exception as exc:
            raise self._map_error(exc) from exc

        yield MessageStart(model=request.model)
        usage = Usage()
        stop_reason = StopReason.END_TURN
        calls: dict[int, dict[str, str]] = {}
        seen: set[int] = set()

        async for event in raw_stream:
            chunk = getattr(event, "data", event)
            if getattr(chunk, "usage", None):
                usage = _to_usage(chunk.usage)
                yield UsageUpdate(usage=usage)
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    text = _delta_text(delta)
                    if text:
                        yield TextDelta(text=text)
                    for position, call in enumerate(getattr(delta, "tool_calls", None) or []):
                        index = getattr(call, "index", None)
                        index = position if index is None else index
                        entry = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                        if getattr(call, "id", None):
                            entry["id"] = call.id
                        function = getattr(call, "function", None)
                        if function is not None and getattr(function, "name", None):
                            entry["name"] = function.name
                        if index not in seen and entry["name"]:
                            seen.add(index)
                            yield ToolCallStart(
                                index=index, id=entry["id"] or entry["name"], name=entry["name"]
                            )
                        arguments = getattr(function, "arguments", None) if function else None
                        if arguments:
                            fragment = (
                                arguments if isinstance(arguments, str) else json.dumps(arguments)
                            )
                            entry["args"] += fragment
                            yield ToolCallDelta(index=index, partial_json=fragment)
                if getattr(choice, "finish_reason", None):
                    stop_reason = _FINISH_REASONS.get(
                        str(choice.finish_reason), StopReason.END_TURN
                    )
                    for index, entry in sorted(calls.items()):
                        yield ToolCallEnd(
                            index=index,
                            id=entry["id"] or entry["name"],
                            name=entry["name"],
                            input=_loads(entry["args"]),
                        )
                    calls.clear()

        yield MessageEnd(stop_reason=stop_reason, usage=usage)

    def _map_error(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        text = str(exc)
        lowered = text.lower()
        if isinstance(exc, TimeoutError | ConnectionError):
            return TransientProviderError(text, provider=self.name, cause=exc)
        if status == 429:
            return RateLimitError(text, provider=self.name, cause=exc)
        if status in (401, 403):
            return AuthenticationError(text, provider=self.name, cause=exc)
        if status == 404:
            return ModelNotFoundError(text, provider=self.name, cause=exc)
        if status == 400:
            if "too large" in lowered or "context" in lowered:
                return ContextLengthError(text, provider=self.name, cause=exc)
            return InvalidRequestError(text, provider=self.name, cause=exc)
        if isinstance(status, int) and status >= 500:
            return TransientProviderError(text, provider=self.name, cause=exc)
        return ProviderError(text, provider=self.name, cause=exc)


def _delta_text(delta: Any) -> str:
    """``content`` is a string, or a list of typed chunks on newer models."""
    content = getattr(delta, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for chunk in content:
        if isinstance(chunk, str):
            parts.append(chunk)
        elif getattr(chunk, "type", None) in (None, "text"):
            parts.append(getattr(chunk, "text", "") or "")
    return "".join(parts)


def _loads(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
        output_tokens=getattr(raw, "completion_tokens", 0) or 0,
    )


def _to_mistral_messages(request: ChatRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        messages.extend(_convert(message))
    return messages


def _convert(message: Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                text_parts.append(block.text)
        elif isinstance(block, ReasoningBlock):
            continue
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
            )
    if text_parts or tool_calls:
        entry: dict[str, Any] = {"role": message.role.value, "content": "".join(text_parts)}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.insert(0, entry)
    return out
