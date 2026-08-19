"""Request-local model streaming events."""

from dataclasses import dataclass

from nano_code.model.primitives import ReasoningDisclosure, ReasoningPresentation
from nano_code.model.request import ModelOutput, ModelReasoningBlock, ModelTextBlock


@dataclass(frozen=True, slots=True)
class ModelTextStarted:
    pass


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelTextCompleted:
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningStarted:
    disclosure: ReasoningDisclosure


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    disclosure: ReasoningDisclosure
    part_index: int
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningCompleted:
    presentation: ReasoningPresentation


@dataclass(frozen=True, slots=True)
class ModelOutputCompleted:
    output: ModelOutput


type ModelStreamPayload = (
    ModelTextStarted
    | ModelTextDelta
    | ModelTextCompleted
    | ModelReasoningStarted
    | ModelReasoningDelta
    | ModelReasoningCompleted
    | ModelOutputCompleted
)


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """A normalized event ordered within one model request."""

    sequence_number: int
    payload: ModelStreamPayload

    def __post_init__(self) -> None:
        if self.sequence_number < 0:
            raise ValueError("Model stream sequence number must not be negative")


@dataclass(slots=True)
class ModelStreamSequencer:
    """Assign contiguous request-local sequence numbers to normalized events."""

    next_sequence: int = 0

    def emit(self, payload: ModelStreamPayload) -> ModelStreamEvent:
        event = ModelStreamEvent(self.next_sequence, payload)
        self.next_sequence += 1
        return event


def completed_output_payloads(output: ModelOutput) -> tuple[ModelStreamPayload, ...]:
    """Project a complete response into display events followed by its snapshot."""

    payloads: list[ModelStreamPayload] = []
    for block in output.content:
        if isinstance(block, ModelTextBlock):
            payloads.extend((ModelTextStarted(), ModelTextCompleted(block.text)))
        elif isinstance(block, ModelReasoningBlock):
            payloads.extend(
                (
                    ModelReasoningStarted(block.presentation.disclosure),
                    ModelReasoningCompleted(block.presentation),
                )
            )
    payloads.append(ModelOutputCompleted(output))
    return tuple(payloads)


__all__ = [
    "ModelOutputCompleted",
    "ModelReasoningCompleted",
    "ModelReasoningDelta",
    "ModelReasoningStarted",
    "ModelStreamEvent",
    "ModelStreamPayload",
    "ModelStreamSequencer",
    "ModelTextCompleted",
    "ModelTextDelta",
    "ModelTextStarted",
    "completed_output_payloads",
]
