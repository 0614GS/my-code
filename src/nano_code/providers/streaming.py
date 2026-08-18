"""Provider-neutral sequencing and final-output display projection."""

from dataclasses import dataclass

from nano_code.agent.contracts.model import (
    ModelOutput,
    ModelOutputCompleted,
    ModelReasoningBlock,
    ModelReasoningCompleted,
    ModelReasoningStarted,
    ModelStreamEvent,
    ModelStreamPayload,
    ModelTextBlock,
    ModelTextCompleted,
    ModelTextStarted,
)


@dataclass(slots=True)
class ModelStreamSequencer:
    """Assign contiguous request-local sequence numbers to normalized events."""

    next_sequence: int = 0

    def emit(self, payload: ModelStreamPayload) -> ModelStreamEvent:
        event = ModelStreamEvent(self.next_sequence, payload)
        self.next_sequence += 1
        return event


def completed_output_payloads(output: ModelOutput) -> tuple[ModelStreamPayload, ...]:
    """Project a complete response into display lifecycles followed by its snapshot."""

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


__all__ = ["ModelStreamSequencer", "completed_output_payloads"]
