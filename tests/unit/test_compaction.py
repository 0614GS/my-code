from collections.abc import AsyncIterator

import pytest

from my_code.context.compaction import (
    CompactionCoordinator,
    CompactionResult,
    CompactionService,
)
from my_code.context.session import ContextSnapshot as ConversationSnapshot
from my_code.conversation.models import AssistantMessage, HumanMessage, TextContent
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelUserMessage,
)


class _CompletionModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelStreamEvent(
            0,
            ModelOutputCompleted(
                ModelOutput(
                    (ModelTextBlock(self.text),),
                    "end_turn",
                    TokenUsage(10, 4, provider_reported=True),
                )
            ),
        )


@pytest.mark.asyncio
async def test_compaction_service_extracts_summary_and_discards_analyze() -> None:
    model = _CompletionModel(
        "preface\n<analyze>draft details that must not survive</analyze>\n"
        "<summary>Continue from verified state.</summary>\ntrailer"
    )
    service = CompactionService(model)

    result = await service.summarize(
        (ModelUserMessage((ModelTextBlock("Fix the parser"),)),)
    )

    assert result.summary == "Continue from verified state."
    assert result.usage.provider_reported is True
    request = model.requests[0]
    assert "<analyze>" in request.system_prompt.text
    final_block = request.messages[-1].content[-1]
    assert isinstance(final_block, ModelTextBlock)
    assert "recent user-authored messages" in final_block.text
    assert request.tools == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "plain summary without tags",
        "<analyze>draft</analyze><summary> </summary>",
        "<summary>one</summary><summary>two</summary>",
    ],
)
async def test_compaction_service_rejects_invalid_summary_contract(
    response: str,
) -> None:
    service = CompactionService(_CompletionModel(response))

    with pytest.raises(RuntimeError, match="exactly one non-empty <summary>"):
        await service.summarize((ModelUserMessage((ModelTextBlock("Keep going"),)),))


class _Context:
    def compaction_view(self, snapshot: ConversationSnapshot):  # type: ignore[no-untyped-def]
        return (ModelUserMessage((ModelTextBlock("model view"),)),), ()

    def measure(self, messages):  # type: ignore[no-untyped-def]
        return 100


class _Summarizer:
    async def summarize(self, messages):  # type: ignore[no-untyped-def]
        return CompactionResult("Generated operational state.", TokenUsage(5, 2))


@pytest.mark.asyncio
async def test_coordinator_appends_recent_real_user_messages_verbatim() -> None:
    first = HumanMessage("Initial request")
    assistant = AssistantMessage(
        (TextContent("working"),),
        TokenUsage(),
        parent_uuid=first.uuid,
    )
    latest = HumanMessage(
        "Correction: preserve this wording exactly.",
        parent_uuid=assistant.uuid,
    )
    snapshot = ConversationSnapshot((first, assistant, latest))
    coordinator = CompactionCoordinator(_Context(), _Summarizer())  # type: ignore[arg-type]

    outcome = await coordinator.compact(snapshot, "manual")

    assert outcome.summary.content.startswith(
        "This session continues from an earlier conversation\nthat was compacted."
    )
    assert "not a new user request" in outcome.summary.content
    assert "## Compacted conversation summary\n\nGenerated operational state." in (
        outcome.summary.content
    )
    assert "### User message 1\nInitial request" in outcome.summary.content
    assert (
        "### User message 2\nCorrection: preserve this wording exactly."
        in outcome.summary.content
    )
    assert "working" not in outcome.summary.content
    assert outcome.summary.parent_uuid == latest.uuid
