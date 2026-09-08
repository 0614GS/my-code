"""请求身份传递及失败、取消语义的回归测试。"""

import asyncio
from collections.abc import AsyncGenerator
from typing import cast
from uuid import uuid4

import pytest

from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelTextStarted,
)
from my_code.model.invocation import (
    ModelInvocation,
    ModelInvocationCoordinator,
    RequestPurpose,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    SystemPrompt,
)
from my_code.sessions.session import Session


class _Client:
    received: ModelRequest | None = None

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    async def stream(self, request: ModelRequest):
        self.received = request
        yield ModelStreamEvent(0, ModelTextStarted())
        if self.error:
            raise self.error
        yield ModelStreamEvent(
            1,
            ModelOutputCompleted(
                ModelOutput(
                    (ModelTextBlock("answer"),),
                    "end_turn",
                    TokenUsage(2, 3, provider_reported=True),
                )
            ),
        )


def _coordinator(tmp_path, client):
    session = Session(tmp_path, str(uuid4()))
    invocation = ModelInvocation(
        ModelRequest(SystemPrompt.from_text("prompt"), (), (), 10),
        (),
        RequestPurpose.AGENT,
        None,
        2,
        attempt=3,
    )
    coordinator = ModelInvocationCoordinator(client, session)
    coordinator.prepare(invocation)
    return coordinator, invocation, session


@pytest.mark.asyncio
async def test_request_identity_matches_audit_without_mutating_input(tmp_path) -> None:
    client = _Client()
    coordinator, invocation, session = _coordinator(tmp_path, client)
    assert [event async for event in coordinator.stream(invocation)]
    assert client.received is not None
    identity = client.received.identity
    assert identity is not None
    assert (
        identity.request_id
        == session.request_audit_snapshot().requests[0].manifest.request_id
    )
    assert (identity.step, identity.attempt, identity.purpose) == (2, 3, "agent")
    assert invocation.request.identity is None
    assert client.received == invocation.request


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LookupError("private"), asyncio.CancelledError()])
async def test_request_failure_and_cancel_are_recorded_without_text(
    tmp_path, error
) -> None:
    coordinator, invocation, session = _coordinator(tmp_path, _Client(error))
    with pytest.raises(type(error)) as caught:
        _ = [event async for event in coordinator.stream(invocation)]
    assert caught.value is error
    manifest = session.request_audit_snapshot().requests[0].manifest
    assert manifest.status == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
    )
    assert manifest.error != "private"


@pytest.mark.asyncio
async def test_early_close_records_cancelled_not_failed(tmp_path) -> None:
    coordinator, invocation, session = _coordinator(tmp_path, _Client())
    stream = cast(
        AsyncGenerator[ModelStreamEvent, None], coordinator.stream(invocation)
    )
    await anext(stream)
    await stream.aclose()
    assert session.request_audit_snapshot().requests[0].manifest.status == "cancelled"
