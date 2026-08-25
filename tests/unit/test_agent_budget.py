"""Cumulative model token budget tests."""

from collections.abc import AsyncIterator

import pytest

from my_code.agent.budget import AgentTokenBudgetExceeded, TokenBudgetModelClient
from my_code.model.client import collect_model_output
from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelStreamSequencer,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    SystemPrompt,
)


class CountingModel:
    def __init__(self) -> None:
        self.requests = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.requests += 1
        response = ModelOutput(
            (ModelTextBlock("done"),),
            "end_turn",
            TokenUsage(4, 2),
        )
        yield ModelStreamSequencer().emit(ModelOutputCompleted(response))


def request() -> ModelRequest:
    return ModelRequest(SystemPrompt.from_text("system"), (), (), 10)


@pytest.mark.asyncio
async def test_budget_keeps_completed_response_and_blocks_next_request() -> None:
    model = CountingModel()
    budgeted = TokenBudgetModelClient(model, max_tokens=5)

    first = await collect_model_output(budgeted, request())

    assert first.content == (ModelTextBlock("done"),)
    assert budgeted.consumed_tokens == 6
    with pytest.raises(AgentTokenBudgetExceeded, match="6/5 tokens"):
        await collect_model_output(budgeted, request())
    assert model.requests == 1


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBudgetModelClient(CountingModel(), 0)
