"""Provider-neutral events emitted while one model response is streaming."""

from dataclasses import dataclass

from nano_code.messages import ModelResponse


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """One display-only text fragment from the provider stream."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelResponseCompleted:
    """The complete response that is safe to validate and persist."""

    response: ModelResponse


type ModelStreamEvent = ModelTextDelta | ModelResponseCompleted
