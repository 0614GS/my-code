"""Provider-neutral model invocation identity and input provenance."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from my_code.model.client import ModelClient
from my_code.model.errors import ModelContextOverflow, ModelProtocolError
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.request import ModelRequest


class RequestPurpose(StrEnum):
    AGENT = "agent"
    CONTINUATION = "continuation"
    COMPACT = "compact"


type ModelInvocationStatus = Literal[
    "completed",
    "failed",
    "cancelled",
    "context-overflow",
    "delivery-unknown",
]


class ModelInputOriginKind(StrEnum):
    USER_CONTEXT = "user_context"
    USER_MESSAGE = "user_message"
    CONVERSATION_ENTRY = "conversation_entry"
    ATTACHMENT = "attachment"
    SUMMARY = "summary"
    CONTENT_REPLACEMENT = "content_replacement"
    COMPACT_INPUT = "compact_input"


@dataclass(frozen=True, slots=True)
class ModelInputOrigin:
    """Where one ordered ``ModelRequest.input`` item came from."""

    kind: ModelInputOriginKind
    source_id: str | None = None
    source: str | None = None
    attachment_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """Immutable semantic request and the facts needed to audit its delivery."""

    request: ModelRequest
    origins: tuple[ModelInputOrigin, ...]
    purpose: RequestPurpose
    causal_head: str | None
    step: int
    attempt: int = 1
    compact_trigger: Literal["manual", "auto", "reactive"] | None = None
    budget: object | None = None
    request_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if len(self.origins) != len(self.request.input):
            raise ValueError("Model invocation origins must match request input")
        if self.step < 1 or self.attempt < 1:
            raise ValueError("Model invocation step and attempt must be positive")
        if self.purpose is RequestPurpose.COMPACT and self.compact_trigger is None:
            raise ValueError("Compact invocation requires a trigger")
        if (
            self.purpose is not RequestPurpose.COMPACT
            and self.compact_trigger is not None
        ):
            raise ValueError("Only compact invocations have a compact trigger")


@dataclass(frozen=True, slots=True)
class ModelInvocationReceipt:
    request_id: str
    request_number: int
    input_refs: tuple[str, ...]


class ModelInvocationRecorder(ABC):
    @abstractmethod
    def prepare_model_invocation(
        self, invocation: ModelInvocation
    ) -> ModelInvocationReceipt:
        raise NotImplementedError

    @abstractmethod
    def finish_model_invocation(
        self,
        request_id: str,
        status: ModelInvocationStatus,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError


class ModelInvocationCoordinator:
    """Enforce audit-before-delivery and append one provider terminal state."""

    def __init__(self, client: ModelClient, recorder: ModelInvocationRecorder) -> None:
        self.client = client
        self.recorder = recorder

    def prepare(self, invocation: ModelInvocation) -> ModelInvocationReceipt:
        return self.recorder.prepare_model_invocation(invocation)

    async def stream(
        self, invocation: ModelInvocation
    ) -> AsyncIterator[ModelStreamEvent]:
        completed_outputs = 0
        try:
            async for event in self.client.stream(invocation.request):
                if isinstance(event.payload, ModelOutputCompleted):
                    completed_outputs += 1
                    if completed_outputs > 1:
                        raise RuntimeError(
                            "Model stream emitted more than one completed output"
                        )
                yield event
                if isinstance(event.payload, ModelOutputCompleted):
                    usage = event.payload.output.usage
                    if not usage.provider_reported or usage.total_input_tokens < 1:
                        raise ModelProtocolError(
                            "Provider completed a response without valid token usage"
                        )
            if completed_outputs != 1:
                raise RuntimeError("Model stream ended without a final response")
        except asyncio.CancelledError:
            self.recorder.finish_model_invocation(invocation.request_id, "cancelled")
            raise
        except ModelContextOverflow:
            self.recorder.finish_model_invocation(
                invocation.request_id, "context-overflow"
            )
            raise
        except BaseException as error:
            self.recorder.finish_model_invocation(
                invocation.request_id, "failed", type(error).__name__
            )
            raise
        else:
            self.recorder.finish_model_invocation(invocation.request_id, "completed")


__all__ = [
    "ModelInputOrigin",
    "ModelInputOriginKind",
    "ModelInvocation",
    "ModelInvocationCoordinator",
    "ModelInvocationRecorder",
    "ModelInvocationReceipt",
    "ModelInvocationStatus",
    "RequestPurpose",
]
