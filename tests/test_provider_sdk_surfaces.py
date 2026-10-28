"""The adapters driven against the *real* vendor SDKs, over a fake transport.

Every other provider test substitutes a fake client --- usually something with
``async def create(**body)`` --- which is fast, offline, and structurally
incapable of catching the one class of bug that actually shipped twice:

* ``providers/anthropic.py`` passed ``temperature`` to ``messages.create``,
  which has no such parameter. A fake that takes ``**body`` accepts it
  happily; the real SDK raises ``TypeError`` before any request is built.
* ``providers/mistral.py`` did ``from mistralai import Mistral``, which is an
  ``ImportError`` --- the top-level package has no re-exports. A fake client
  means the real import is never executed.

So these tests keep the real SDK object and replace only the socket, with a
mock transport that records the outgoing request and returns a canned SSE
stream. That covers the two things a fake cannot: that the arguments we pass
are arguments the SDK accepts, and that what we intend to send actually
reaches the wire.

The transport comes from ``httpx2``, not ``httpx``: the Anthropic SDK moved
HTTP stacks and now rejects an ``httpx`` object outright rather than failing
somewhere inside a request. Nothing in ``src/`` is affected --- the Anthropic
adapter builds no client of its own --- but ``openai``, ``local`` and
``azure_foundry`` all pass an ``httpx.AsyncClient`` as ``http_client``, so if
that SDK ever follows, this is the paragraph to reread.

No network: the transport never opens a connection, so this runs in CI and
costs nothing. What it cannot prove is that the vendor *honours* a field it
receives --- only that we send it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from altus.config.models import ProviderSettings
from altus.core.types import ChatRequest, Message, Role, TextBlock

ANTHROPIC_SSE = (
    "\n\n".join(
        [
            "event: message_start\ndata: "
            + json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 3, "output_tokens": 0},
                    },
                }
            ),
            "event: content_block_start\ndata: "
            + json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"},
                }
            ),
            "event: content_block_stop\ndata: "
            + json.dumps({"type": "content_block_stop", "index": 0}),
            "event: message_delta\ndata: "
            + json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                }
            ),
            "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}),
        ]
    )
    + "\n\n"
)


def _request() -> ChatRequest:
    return ChatRequest(
        model="claude-opus-5",
        messages=[Message(role=Role.USER, content=[TextBlock(text="hi")])],
        max_tokens=64,
        temperature=0.3,
        top_p=0.9,
        system="be brief",
    )


class Recorder:
    """Captures the one request the SDK makes, and answers it."""

    def __init__(self, body: str, *, content_type: str) -> None:
        self.seen: list[Any] = []
        self._body = body
        self._content_type = content_type

    def __call__(self, request: Any) -> Any:
        import httpx2

        self.seen.append(request)
        return httpx2.Response(
            200, content=self._body, headers={"content-type": self._content_type}
        )

    @property
    def payload(self) -> dict[str, Any]:
        assert self.seen, "the SDK made no request"
        return dict(json.loads(self.seen[-1].content))


# --------------------------------------------------------------------- anthropic


@pytest.fixture
def anthropic_provider() -> tuple[Any, Recorder]:
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from altus.providers.anthropic import AnthropicProvider

    recorder = Recorder(ANTHROPIC_SSE, content_type="text/event-stream")
    provider = AnthropicProvider(api_key="test-key", settings=ProviderSettings())
    # The real SDK client, with only its socket replaced. Assigned directly
    # because the point of the test is that `_get_client`'s product is used.
    provider._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
    )
    return provider, recorder


async def test_the_anthropic_sdk_accepts_everything_the_adapter_passes(
    anthropic_provider: tuple[Any, Recorder],
) -> None:
    """The bug this replaces: `temperature=` is not a parameter of
    `messages.create`, so the first turn of any profile with a temperature
    raised TypeError. A fake client taking **body could never see it."""
    provider, _ = anthropic_provider
    events = [event async for event in provider.stream(_request())]
    assert events, "the adapter produced no events"


async def test_the_sampling_parameters_reach_the_wire(
    anthropic_provider: tuple[Any, Recorder],
) -> None:
    """`extra_body` is only the right fix if the SDK merges it into the
    payload, so this asserts on the bytes rather than on the call."""
    provider, recorder = anthropic_provider
    [_ async for _ in provider.stream(_request())]

    payload = recorder.payload
    assert payload["temperature"] == 0.3
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 64
    assert payload["system"] == "be brief"
    assert payload["stream"] is True
    assert "extra_body" not in payload


async def test_omitted_sampling_parameters_are_not_sent_at_all(
    anthropic_provider: tuple[Any, Recorder],
) -> None:
    """A default profile sets neither, and sending `null` is not the same as
    not sending the field."""
    provider, recorder = anthropic_provider
    bare = ChatRequest(
        model="claude-opus-5",
        messages=[Message(role=Role.USER, content=[TextBlock(text="hi")])],
        max_tokens=16,
    )
    [_ async for _ in provider.stream(bare)]

    payload = recorder.payload
    assert "temperature" not in payload
    assert "top_p" not in payload


async def test_the_adapter_still_normalises_a_real_sdk_stream(
    anthropic_provider: tuple[Any, Recorder],
) -> None:
    """The response half of the surface: the SDK's own event objects, not our
    fixtures, through the adapter's normaliser."""
    from altus.core.events import MessageEnd, MessageStart, TextDelta, UsageUpdate

    provider, _ = anthropic_provider
    events = [event async for event in provider.stream(_request())]
    kinds = [type(event) for event in events]

    assert kinds[0] is MessageStart
    assert TextDelta in kinds
    assert UsageUpdate in kinds
    assert kinds[-1] is MessageEnd
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello"


# ----------------------------------------------------------------------- mistral


def test_the_mistral_client_can_actually_be_imported() -> None:
    """`from mistralai import Mistral` is an ImportError: the top-level
    package is a namespace package with no re-exports. The lazy import plus a
    fake client meant this was never executed in a test."""
    pytest.importorskip("mistralai")
    from altus.providers.mistral import MistralProvider

    provider = MistralProvider(api_key="test-key", settings=ProviderSettings())
    client = provider._get_client()

    assert client is not None
    assert hasattr(client.chat, "stream_async")
    # Cached, so a second turn does not rebuild it.
    assert provider._get_client() is client


def test_the_mistral_sdk_accepts_the_arguments_the_adapter_passes() -> None:
    """Checked by binding against the real signature rather than by calling:
    the mistral client builds its own transport, so there is no socket to
    replace without reaching further into the SDK than a test should."""
    import inspect

    pytest.importorskip("mistralai")
    from altus.providers.mistral import MistralProvider

    provider = MistralProvider(api_key="test-key", settings=ProviderSettings())
    signature = inspect.signature(provider._get_client().chat.stream_async)

    for name in ("model", "messages", "max_tokens", "temperature", "top_p", "stop", "tools"):
        assert name in signature.parameters, f"stream_async lost {name}"
