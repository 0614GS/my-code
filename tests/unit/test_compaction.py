from collections.abc import AsyncIterator
from typing import cast

import pytest

from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextPlanningState
from my_code.conversation.models import AssistantMessage, HumanMessage, TextContent
from my_code.model.capabilities import (
    ActiveModelEnvironment,
    ModelDescriptor,
    ModelLimits,
)
from my_code.model.errors import ModelContextOverflow
from my_code.model.events import ModelOutputCompleted, ModelStreamEvent
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutput,
    ToolOutputs,
    ToolOutputText,
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

    assert summary == "preface\n\n\nContinue from verified state.\n\ntrailer"
    assert usage.provider_reported is True
    request = model.requests[0]
    assert "<summary>" not in request.system_prompt.text
    assert "Markdown handoff" in request.system_prompt.text
    final_block = request.input[-1].content[-1]  # type: ignore[union-attr]
    assert isinstance(final_block, InputText)
    assert "recent user-authored messages" in final_block.text
    assert request.tools == ()
    assert request.max_output_tokens == 20_000
    assert request.reasoning_mode == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "<analyze>draft</analyze><summary> </summary>",
        "<summary>one</summary><summary>two</summary>",
        "<summary>missing close",
        "<summary>valid</summary><summary",
    ],
)
async def test_compaction_service_accepts_legacy_summary_shapes(response: str) -> None:
    service = ContextCompactor(_CompletionModel(response))

    if response.startswith("<analyze>"):
        with pytest.raises(RuntimeError, match="empty summary"):
            await service.summarize((UserInput((InputText("Keep going"),)),))
    else:
        summary, _ = await service.summarize((UserInput((InputText("Keep going"),)),))
        assert summary
        assert "summary" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("plain summary without tags", "plain summary without tags"),
        ("```text\nfenced summary\n```", "fenced summary"),
        (
            "<analyze>discard this</analyze>\ncompatible summary",
            "compatible summary",
        ),
        (
            "<analysis>first draft</analysis><analyze>second draft</analyze>final",
            "final",
        ),
    ],
)
async def test_compaction_service_accepts_unambiguous_plain_fallback(
    response: str, expected: str
) -> None:
    summary, _ = await ContextCompactor(_CompletionModel(response)).summarize(
        (UserInput((InputText("Keep going"),)),)
    )

    assert summary == expected


def test_compaction_service_rejects_empty_normalized_text() -> None:
    with pytest.raises(RuntimeError, match="empty summary"):
        from my_code.context.compaction import _extract_summary

        _extract_summary("<analysis>draft only</analysis><summary> </summary>")


class _ScriptedModel:
    def __init__(self, results: list[ModelOutput | Exception]) -> None:
        self.results = results
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        yield ModelStreamEvent(0, ModelOutputCompleted(result))


def _output(
    text: str,
    stop_reason: str,
    usage: TokenUsage,
) -> ModelOutput:
    return ModelOutput((ModelTextBlock(text),), stop_reason, usage)


@pytest.mark.asyncio
async def test_compaction_retries_truncation_and_accumulates_usage() -> None:
    model = _ScriptedModel(
        [
            _output(
                "partial",
                "max_tokens",
                TokenUsage(10, 20, provider_reported=True),
            ),
            _output("complete", "end_turn", TokenUsage(11, 5, provider_reported=True)),
        ]
    )

    summary, usage = await ContextCompactor(model).summarize(
        (UserInput((InputText("task"),)),)
    )

    assert summary == "complete"
    assert len(model.requests) == 2
    assert model.requests[0].input[:-1] == model.requests[1].input[:-1]
    retry = cast(UserInput, model.requests[1].input[-1]).content[-1]
    assert isinstance(retry, InputText)
    assert "16,000 tokens" in retry.text
    assert usage == TokenUsage(21, 25, provider_reported=True)


@pytest.mark.asyncio
async def test_compaction_fails_after_two_truncated_outputs() -> None:
    model = _ScriptedModel(
        [
            _output(
                "partial",
                "max_output_tokens",
                TokenUsage(1, 20, provider_reported=True),
            ),
            _output(
                "still partial",
                "max_output_tokens",
                TokenUsage(1, 20, provider_reported=True),
            ),
        ]
    )

    with pytest.raises(
        RuntimeError,
        match=r"truncated after 2 attempts .*max_output_tokens=20000",
    ):
        await ContextCompactor(model).summarize((UserInput((InputText("task"),)),))


@pytest.mark.asyncio
async def test_compaction_caps_output_to_known_model_limit() -> None:
    environment = ActiveModelEnvironment(
        ModelDescriptor("model", "Model", ModelLimits(max_output_tokens=12_000)),
        compact_trigger_tokens=100,
    )
    model = _CompletionModel("done")

    await ContextCompactor(
        model,
        model_environment=lambda: environment,
    ).summarize((UserInput((InputText("task"),)),))

    assert model.requests[0].max_output_tokens == 12_000


def _tool_turn(index: int) -> tuple[UserInput, AssistantOutput, ToolOutputs]:
    call_id = f"call-{index}"
    return (
        UserInput((InputText(f"user-{index}"),)),
        AssistantOutput((ModelToolUseBlock(call_id, "Read", {"index": index}),)),
        ToolOutputs((ToolOutput(call_id, (ToolOutputText(f"result-{index}"),)),)),
    )


@pytest.mark.asyncio
async def test_compaction_crops_oldest_complete_turns_after_input_overflow() -> None:
    model = _ScriptedModel(
        [
            ModelContextOverflow("too long"),
            ModelContextOverflow("still too long"),
            _output("done", "end_turn", TokenUsage(2, 1, provider_reported=True)),
        ]
    )
    messages = tuple(item for index in range(5) for item in _tool_turn(index))

    summary, usage = await ContextCompactor(model).summarize(messages)

    assert summary == "done"
    assert usage == TokenUsage(2, 1, provider_reported=True)
    final_input = model.requests[-1].input
    rendered_users = [
        block.text
        for item in final_input
        if isinstance(item, UserInput)
        for block in item.content
        if isinstance(block, InputText)
    ]
    assert rendered_users[0].startswith("[Earlier context was omitted")
    assert "user-0" not in rendered_users
    assert "user-1" not in rendered_users
    assert "user-4" in rendered_users
    final_calls = [
        block.id
        for item in final_input
        if isinstance(item, AssistantOutput)
        for block in item.content
        if isinstance(block, ModelToolUseBlock)
    ]
    final_outputs = [
        result.call_id
        for item in final_input
        if isinstance(item, ToolOutputs)
        for result in item.results
    ]
    assert final_calls == final_outputs == ["call-2", "call-3", "call-4"]


@pytest.mark.asyncio
async def test_compaction_overflow_fails_when_only_latest_turn_remains() -> None:
    model = _ScriptedModel([ModelContextOverflow("too long")])

    with pytest.raises(ModelContextOverflow, match="latest safe conversation turn"):
        await ContextCompactor(model).summarize(_tool_turn(0))


@pytest.mark.asyncio
async def test_compaction_stops_after_three_input_cropping_retries() -> None:
    model = _ScriptedModel([ModelContextOverflow("too long") for _ in range(4)])
    messages = tuple(item for index in range(10) for item in _tool_turn(index))

    with pytest.raises(ModelContextOverflow, match="after 3 cropping retries"):
        await ContextCompactor(model).summarize(messages)

    assert len(model.requests) == 4
    latest_users = [
        block.text
        for item in model.requests[-1].input
        if isinstance(item, UserInput)
        for block in item.content
        if isinstance(block, InputText)
    ]
    assert "user-9" in latest_users


class _Context:
    def compaction_view(self, state: ContextPlanningState):  # type: ignore[no-untyped-def]
        return (UserInput((InputText("model view"),)),), ()

    def measure(self, messages):  # type: ignore[no-untyped-def]
        return 100, "estimated"


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
