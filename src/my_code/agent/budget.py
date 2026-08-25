"""Provider-neutral cumulative token budget for one Agent run."""

from __future__ import annotations

from collections.abc import AsyncIterator

from my_code.model.client import ModelClient
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.request import ModelRequest


class AgentTokenBudgetExceeded(RuntimeError):
    """A run attempted another model request after exhausting its budget."""


class TokenBudgetModelClient(ModelClient):
    """Count completed request usage and reject requests beyond a run budget.

    A response already received from a provider remains usable. The budget is
    checked before the next request, avoiding a paid response being discarded.
    """

    def __init__(self, client: ModelClient, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("Agent token budget must be positive")
        self.client = client
        self.max_tokens = max_tokens
        self.consumed_tokens = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        if self.consumed_tokens >= self.max_tokens:
            raise AgentTokenBudgetExceeded(
                "Agent token budget exhausted: "
                f"{self.consumed_tokens}/{self.max_tokens} tokens"
            )
        async for event in self.client.stream(request):
            if isinstance(event.payload, ModelOutputCompleted):
                usage = event.payload.output.usage
                self.consumed_tokens += usage.total_input_tokens + usage.output_tokens
            yield event


__all__ = ["AgentTokenBudgetExceeded", "TokenBudgetModelClient"]
