from collections.abc import AsyncIterator
from typing import cast

import pytest

from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextPlanningState
from my_code.conversation.models import AssistantMessage, HumanMessage, TextContent
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    InputText,
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    UserInput,
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
    service = ContextCompactor(model)

    summary, usage = await service.summarize(
        (UserInput((InputText("Fix the parser"),)),)
    )

    assert summary == "Continue from verified state."
    assert usage.provider_reported is True
    request = model.requests[0]
    assert "<analyze>" in request.system_prompt.text
    final_block = request.input[-1].content[-1]  # type: ignore[union-attr]
    assert isinstance(final_block, InputText)
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
    service = ContextCompactor(_CompletionModel(response))

    with pytest.raises(RuntimeError, match="exactly one non-empty <summary>"):
        await service.summarize((UserInput((InputText("Keep going"),)),))


class _Context:
    def compaction_view(self, state: ContextPlanningState):  # type: ignore[no-untyped-def]
        return (UserInput((InputText("model view"),)),), ()

    def measure(self, messages):  # type: ignore[no-untyped-def]
        return 100


@pytest.mark.asyncio
async def test_context_engine_appends_recent_real_user_messages_verbatim() -> None:
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
    state = ContextPlanningState((first, assistant, latest))
    model = _CompletionModel(
        "<analyze>complete</analyze><summary>Generated operational state.</summary>"
    )
    context = ContextEngine(
        cast(ContextPlanner, _Context()),
        ContextCompactor(model),
    )

    outcome = await context.compact(state, "manual")

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
