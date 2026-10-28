"""Normalizer tests for the Anthropic adapter. No network, no SDK objects."""

from __future__ import annotations

from typing import Any

import pytest

from altus.config.models import ProviderSettings
from altus.core.events import (
    MessageEnd,
    MessageStart,
    ReasoningDelta,
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
    ReasoningBlock,
    Role,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from altus.providers.anthropic import AnthropicProvider, _to_anthropic_message
from tests.fixtures import anthropic_raw as raw


class _FakeMessages:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.body: dict[str, Any] = {}

    async def create(self, **body: Any) -> Any:
        self.body = body
        return _AsyncList(self.events)


class _AsyncList:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for item in self.items:
                yield item

        return gen()


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test", settings=ProviderSettings())


def _wire(provider: AnthropicProvider, events: list[Any]) -> _FakeMessages:
    messages = _FakeMessages(events)
    provider._get_client = lambda: type("C", (), {"messages": messages})()  # type: ignore[method-assign]
    return messages


def _request(**kwargs: Any) -> ChatRequest:
    defaults: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "messages": [Message.user("hi")],
        "max_tokens": 1024,
    }
    return ChatRequest(**{**defaults, **kwargs})


async def _collect(provider: AnthropicProvider, request: ChatRequest) -> list[Any]:
    return [event async for event in provider.stream(request)]


async def test_text_turn_normalizes(provider: AnthropicProvider) -> None:
    _wire(provider, raw.TEXT_TURN)
    events = await _collect(provider, _request())
    assert isinstance(events[0], MessageStart)
    assert events[0].usage.input_tokens == 12
    assert events[0].usage.cache_read_tokens == 3
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"
    assert isinstance(events[-1], MessageEnd)
    assert events[-1].stop_reason is StopReason.END_TURN
    assert events[-1].usage.output_tokens == 7


async def test_reasoning_and_signature(provider: AnthropicProvider) -> None:
    _wire(provider, raw.REASONING_TURN)
    events = await _collect(provider, _request())
    reasoning = [e for e in events if isinstance(e, ReasoningDelta)]
    assert reasoning[0].text == "let me think"
    assert reasoning[1].signature == "sig-abc"
    assert any(isinstance(e, TextDelta) and e.text == "answer" for e in events)


async def test_tool_call_is_reassembled(provider: AnthropicProvider) -> None:
    """Phase 1 never sends tools, but the contract must already hold."""
    _wire(provider, raw.TOOL_TURN)
    events = await _collect(provider, _request())
    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert (start.id, start.name) == ("toolu_1", "run_command")
    assert [e.partial_json for e in events if isinstance(e, ToolCallDelta)] == [
        '{"cmd":',
        ' "kubectl get pods"}',
    ]
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"cmd": "kubectl get pods"}
    assert next(e for e in events if isinstance(e, MessageEnd)).stop_reason is StopReason.TOOL_USE


async def test_truncated_tool_json_does_not_crash(provider: AnthropicProvider) -> None:
    events_in = [
        raw.message_start(),
        raw.tool_block_start(0, "t", "n"),
        raw.input_json_delta(0, '{"broken'),
        raw.block_stop(0),
        raw.message_delta(),
        raw.message_stop(),
    ]
    _wire(provider, events_in)
    events = await _collect(provider, _request())
    assert next(e for e in events if isinstance(e, ToolCallEnd)).input == {}


async def test_max_tokens_stop_reason(provider: AnthropicProvider) -> None:
    _wire(provider, raw.MAX_TOKENS_TURN)
    events = await _collect(provider, _request())
    assert next(e for e in events if isinstance(e, MessageEnd)).stop_reason is StopReason.MAX_TOKENS


async def test_usage_update_is_emitted(provider: AnthropicProvider) -> None:
    _wire(provider, raw.TEXT_TURN)
    events = await _collect(provider, _request())
    assert any(isinstance(e, UsageUpdate) for e in events)


async def test_request_body_shape(provider: AnthropicProvider) -> None:
    messages = _wire(provider, raw.TEXT_TURN)
    await _collect(
        provider,
        _request(system="be terse", temperature=0.2, stop_sequences=["STOP"], extra={"top_k": 5}),
    )
    body = messages.body
    assert body["system"] == "be terse"
    assert body["max_tokens"] == 1024
    # The sampling parameters are not parameters of the SDK's `messages.create`,
    # so they travel in `extra_body`, which it merges into the payload. See
    # tests/test_provider_sdk_surfaces.py, which pins that against the real SDK
    # --- this fake takes **body and would accept them either way.
    assert body["extra_body"] == {"temperature": 0.2}
    assert body["stop_sequences"] == ["STOP"]
    assert body["top_k"] == 5, "extra must pass through"
    assert body["stream"] is True
    assert "tools" not in body


def test_message_translation_covers_every_block() -> None:
    message = Message(
        role=Role.ASSISTANT,
        content=[
            ReasoningBlock(text="t", signature="s"),
            TextBlock(text="body"),
            ToolUseBlock(id="u1", name="n", input={"a": 1}),
        ],
    )
    out = _to_anthropic_message(message)
    assert out["role"] == "assistant"
    assert [b["type"] for b in out["content"]] == ["thinking", "text", "tool_use"]
    assert out["content"][0]["signature"] == "s"


def test_message_translation_image_and_tool_result() -> None:
    message = Message(
        role=Role.USER,
        content=[
            ToolResultBlock(tool_use_id="u1", content="ok"),
            ImageBlock(media_type="image/png", data="Zm9v"),
        ],
    )
    out = _to_anthropic_message(message)
    assert out["content"][0]["tool_use_id"] == "u1"
    assert out["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "Zm9v",
    }


def test_empty_text_blocks_are_dropped() -> None:
    """Anthropic rejects empty text blocks."""
    out = _to_anthropic_message(Message(role=Role.USER, content=[TextBlock(text="")]))
    assert out["content"] == []


def test_capabilities_declare_anthropic_quirks() -> None:
    caps = AnthropicProvider.capabilities
    assert caps.requires_max_tokens is True
    assert caps.system_as_field is True
    assert caps.reasoning is True
