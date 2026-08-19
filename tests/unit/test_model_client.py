from collections.abc import AsyncIterator

import pytest

from nano_code.model import (
    ModelOutput,
    ModelOutputCompleted,
    ModelRequest,
    ModelStreamEvent,
    ModelTextBlock,
    SystemPrompt,
    collect_model_output,
)


def _request() -> ModelRequest:
    return ModelRequest(SystemPrompt.from_text("system"), (), (), 10)


class _StreamClient:
    def __init__(self, *events: ModelStreamEvent) -> None:
        self.events = events

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        for event in self.events:
            yield event


def _completed(sequence_number: int = 0) -> ModelStreamEvent:
    return ModelStreamEvent(
        sequence_number,
        ModelOutputCompleted(ModelOutput((ModelTextBlock("done"),), "end_turn")),
    )


@pytest.mark.asyncio
async def test_collect_model_output_returns_the_unique_final_snapshot() -> None:
    event = _completed()
    assert isinstance(event.payload, ModelOutputCompleted)
    assert await collect_model_output(_StreamClient(event), _request()) == (
        event.payload.output
    )


@pytest.mark.asyncio
async def test_collect_model_output_rejects_missing_or_duplicate_snapshots() -> None:
    with pytest.raises(RuntimeError, match="without a completed output"):
        await collect_model_output(_StreamClient(), _request())

    with pytest.raises(RuntimeError, match="more than one completed output"):
        await collect_model_output(
            _StreamClient(_completed(), _completed(1)),
            _request(),
        )
