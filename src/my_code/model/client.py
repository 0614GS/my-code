"""The single polymorphic model-call capability."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from my_code.model.errors import ModelProtocolError
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.request import ModelOutput, ModelRequest


@runtime_checkable
class ModelClient(Protocol):
    """Stream one request as provider-neutral ordered events."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


async def collect_model_output(
    client: ModelClient, request: ModelRequest
) -> ModelOutput:
    """Collect the unique final output carried by a model stream."""

    output: ModelOutput | None = None
    async for event in client.stream(request):
        if not isinstance(event.payload, ModelOutputCompleted):
            continue
        if output is not None:
            raise RuntimeError("Model stream emitted more than one completed output")
        output = event.payload.output
    if output is None:
        raise RuntimeError("Model stream ended without a completed output")
    if not output.usage.provider_reported or output.usage.total_input_tokens < 1:
        raise ModelProtocolError(
            "Provider completed a response without valid token usage"
        )
    return output


__all__ = [
    "ModelClient",
    "collect_model_output",
]
