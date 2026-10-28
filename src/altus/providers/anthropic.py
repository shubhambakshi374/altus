"""Anthropic adapter.

System prompt is a separate field, ``max_tokens`` is mandatory, and extended
thinking arrives as ``thinking``/``signature`` deltas which must be echoed back
verbatim on the following turn.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar

from altus.core.errors import (
    AuthenticationError,
    ContentFilterError,
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
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageUpdate,
)
from altus.core.types import (
    ChatRequest,
    ImageBlock,
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
    from anthropic import AsyncAnthropic

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "tool_use": StopReason.TOOL_USE,
    "pause_turn": StopReason.END_TURN,
    "refusal": StopReason.CONTENT_FILTER,
}


class AnthropicProvider(BaseProvider):
    name: ClassVar[str] = "anthropic"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        streaming=True,
        tools=True,
        parallel_tool_calls=True,
        reasoning=True,
        vision=True,
        system_as_field=True,
        requires_max_tokens=True,
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic

            from altus.config.secrets import require_api_key

            self._client = AsyncAnthropic(
                api_key=self.api_key or require_api_key(self.name),
                base_url=self.settings.base_url,
                timeout=self.settings.timeout,
                max_retries=0,  # altus.core.retry owns retry policy
                default_headers=self.settings.extra_headers or None,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def list_models(self) -> list[ModelInfo]:
        from altus.providers.registry import catalog_for

        catalog = {m.id: m for m in catalog_for(self.name)}
        try:
            page = await self._get_client().models.list(limit=100)
        except Exception as exc:
            raise _map_error(exc) from exc
        models: list[ModelInfo] = []
        for item in page.data:
            known = catalog.get(item.id)
            models.append(
                known
                or ModelInfo(
                    id=item.id,
                    provider=self.name,
                    display_name=getattr(item, "display_name", None),
                    context_window=200_000,
                    max_output_tokens=8_192,
                    supports_vision=True,
                )
            )
        return models

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        client = self._get_client()
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [_to_anthropic_message(m) for m in request.messages],
        }
        if request.system:
            body["system"] = request.system
        # The sampling parameters are not in the SDK's typed `messages.create`
        # --- they were already absent at the version this adapter was written
        # against, so `temperature=...` was a TypeError on the first turn of
        # any profile that set one, and nothing caught it because the suite
        # drives a fake provider. They are still API fields, so they travel in
        # `extra_body`, which the SDK merges into the request payload.
        sampling: dict[str, Any] = {}
        if request.temperature is not None:
            sampling["temperature"] = request.temperature
        if request.top_p is not None:
            sampling["top_p"] = request.top_p
        if sampling:
            body["extra_body"] = sampling
        if request.stop_sequences:
            body["stop_sequences"] = request.stop_sequences
        if request.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        body.update(request.extra)

        try:
            raw_stream = await client.messages.create(stream=True, **body)
        except Exception as exc:
            raise _map_error(exc) from exc

        usage = Usage()
        stop_reason = StopReason.END_TURN
        tool_calls: dict[int, dict[str, Any]] = {}

        async for event in raw_stream:
            kind = event.type
            if kind == "message_start":
                usage = _to_usage(event.message.usage)
                yield MessageStart(model=event.message.model, usage=usage)
            elif kind == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    tool_calls[event.index] = {"id": block.id, "name": block.name, "json": ""}
                    yield ToolCallStart(index=event.index, id=block.id, name=block.name)
            elif kind == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield TextDelta(text=delta.text)
                elif delta.type == "thinking_delta":
                    yield ReasoningDelta(text=delta.thinking)
                elif delta.type == "signature_delta":
                    yield ReasoningDelta(text="", signature=delta.signature)
                elif delta.type == "input_json_delta":
                    call = tool_calls.get(event.index)
                    if call is not None:
                        call["json"] += delta.partial_json
                    yield ToolCallDelta(index=event.index, partial_json=delta.partial_json)
            elif kind == "content_block_stop":
                call = tool_calls.pop(event.index, None)
                if call is not None:
                    yield ToolCallEnd(
                        index=event.index,
                        id=call["id"],
                        name=call["name"],
                        input=_loads(call["json"]),
                    )
            elif kind == "message_delta":
                if event.delta.stop_reason:
                    stop_reason = _STOP_REASONS.get(event.delta.stop_reason, StopReason.END_TURN)
                usage.output_tokens = event.usage.output_tokens
                yield UsageUpdate(usage=usage)
            elif kind == "message_stop":
                yield MessageEnd(stop_reason=stop_reason, usage=usage)


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
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )


def _to_anthropic_message(message: Message) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ReasoningBlock):
            if block.redacted:
                blocks.append({"type": "redacted_thinking", "data": block.signature or ""})
            elif block.text:
                blocks.append(
                    {"type": "thinking", "thinking": block.text, "signature": block.signature or ""}
                )
        elif isinstance(block, ToolUseBlock):
            blocks.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
        elif isinstance(block, ImageBlock):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    },
                }
            )
    return {"role": message.role.value, "content": blocks}


def _map_error(exc: Exception) -> Exception:
    import anthropic

    provider = AnthropicProvider.name
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return TransientProviderError(str(exc), provider=provider, cause=exc)
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitError(str(exc), provider=provider, retry_after=_retry_after(exc), cause=exc)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AuthenticationError(str(exc), provider=provider, cause=exc)
    if isinstance(exc, anthropic.NotFoundError):
        return ModelNotFoundError(str(exc), provider=provider, cause=exc)
    if isinstance(exc, anthropic.BadRequestError):
        text = str(exc).lower()
        if "context" in text or "too long" in text or "max_tokens" in text:
            return ContextLengthError(str(exc), provider=provider, cause=exc)
        if "refus" in text or "safety" in text:
            return ContentFilterError(str(exc), provider=provider, cause=exc)
        return InvalidRequestError(str(exc), provider=provider, cause=exc)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return TransientProviderError(str(exc), provider=provider, cause=exc)
        return ProviderError(str(exc), provider=provider, cause=exc)
    return exc


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    try:
        return float(header) if header else None
    except TypeError, ValueError:
        return None
