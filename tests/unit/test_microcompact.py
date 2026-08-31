from datetime import UTC, datetime, timedelta

from my_code.context.microcompact import MicrocompactPolicy
from my_code.conversation.models import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.model.primitives import TokenUsage

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _messages(names: tuple[str, ...], *, age_minutes: int = 31):
    messages = []
    parent = None
    for index, name in enumerate(names):
        call_id = f"call-{index}"
        assistant = AssistantMessage(
            (ToolCall(call_id, name, {}),),
            TokenUsage(),
            parent_uuid=parent,
        )
        result_text = f"result-{index}-" + "x" * 100
        result = ToolResult(
            call_id,
            result_text,
            generic_tool_result_presentation(result_text, False),
        )
        batch = ToolResultBatch(
            (result,),
            assistant.uuid,
            parent_uuid=assistant.uuid,
            timestamp=(NOW - timedelta(minutes=age_minutes)).isoformat(),
        )
        messages.extend((assistant, batch))
        parent = batch.uuid
    return tuple(messages)


def _estimate(view) -> int:  # type: ignore[no-untyped-def]
    return sum(
        len(result.content)
        for message in view
        if isinstance(message, ToolResultBatch)
        for result in message.content
    )


def test_microcompact_keeps_recent_five_and_clears_oldest_first() -> None:
    messages = _messages(("Read",) * 7)
    policy = MicrocompactPolicy(now=lambda: NOW)
    replacements = policy.propose(
        messages,
        (),
        current_tokens=_estimate(messages),
        trigger_tokens=1,
        estimate=_estimate,
    )
    assert [item.tool_use_id for item in replacements] == ["call-0", "call-1"]


def test_microcompact_age_boundary_is_inclusive() -> None:
    messages = _messages(("Read",) * 6, age_minutes=30)
    replacements = MicrocompactPolicy(now=lambda: NOW).propose(
        messages,
        (),
        current_tokens=_estimate(messages),
        trigger_tokens=1,
        estimate=_estimate,
    )
    assert [item.tool_use_id for item in replacements] == ["call-0"]


def test_microcompact_excludes_tools_outside_allowlist() -> None:
    messages = _messages(("Bash", "Write", "Grep", "Glob", "Read", "Grep", "Glob"))
    replacements = MicrocompactPolicy(now=lambda: NOW).propose(
        messages,
        (),
        current_tokens=_estimate(messages),
        trigger_tokens=1,
        estimate=_estimate,
    )
    assert replacements == ()


def test_microcompact_does_not_commit_a_non_saving_replacement() -> None:
    messages = _messages(("Read",) * 6)
    replacements = MicrocompactPolicy(now=lambda: NOW).propose(
        messages,
        (),
        current_tokens=100,
        trigger_tokens=100,
        estimate=lambda view: 100,
    )
    assert replacements == ()
