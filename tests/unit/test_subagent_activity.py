"""Safe, ordered child-agent transcript projection tests."""

from my_code.agent.events import (
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentTextCompleted,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.subagents.activity import SubagentActivityRecord
from my_code.features.subagents.models import SubagentType
from my_code.model.primitives import ReasoningPresentation
from my_code.tasks.models import (
    SubagentToolResultView,
    SubagentTranscriptReasoning,
    SubagentTranscriptText,
    SubagentTranscriptTool,
    TaskSnapshot,
    TaskStatus,
)
from my_code.tools.presentation import ToolUsePresentation


def record(task_id: str = "task-1") -> SubagentActivityRecord:
    return SubagentActivityRecord(
        task_id,
        f"run-{task_id}",
        "owner",
        SubagentType.GENERAL,
        "Inspect the project",
        False,
        "Read the source and report.",
    )


def snapshot(task_id: str = "task-1") -> TaskSnapshot:
    return TaskSnapshot(
        task_id,
        "subagent:test",
        None,
        TaskStatus.RUNNING,
        "2026-08-27T00:00:00+00:00",
        "2026-08-27T00:00:00+00:00",
    )


def test_transcript_projects_prompt_live_streams_and_paired_tools_safely() -> None:
    activity = record()
    activity.consume(AgentTextDelta("Working **now**"))
    live = activity.view(snapshot())

    assert live.transcript == (
        SubagentTranscriptText("user", "Read the source and report."),
        SubagentTranscriptText("assistant", "Working **now**", streaming=True),
    )

    activity.consume(AgentTextCompleted("Working **now**"))
    activity.consume(AgentReasoningDelta("summary", 0, "Checking boundaries"))
    activity.consume(
        AgentReasoningCompleted(ReasoningPresentation("summary", ("Safe summary",)))
    )
    activity.consume(AgentReasoningCompleted(ReasoningPresentation("hidden")))
    activity.consume(
        AgentToolStarted(
            "tool-1",
            "Read",
            {"path": "secret-input-must-not-leak"},
            ToolUsePresentation("Read", "README.md", "Reading README.md"),
        )
    )
    running = activity.view(snapshot())
    assert running.active_tool_ids == ("tool-1",)
    assert "secret-input-must-not-leak" not in repr(running)

    activity.consume(
        AgentToolFinished(
            "tool-1",
            "Read",
            True,
            ToolResultPresentation("Read failed", "permission denied"),
        )
    )
    final = activity.view(snapshot())

    assert isinstance(final.transcript[2], SubagentTranscriptReasoning)
    assert isinstance(final.transcript[3], SubagentTranscriptReasoning)
    assert final.transcript[3].disclosure == "hidden"
    tool = final.transcript[4]
    assert isinstance(tool, SubagentTranscriptTool)
    assert tool.result == SubagentToolResultView("Read failed", "permission denied")
    assert tool.is_error is True
    assert final.active_tool_ids == ()


def test_concurrent_subagent_streams_remain_isolated() -> None:
    first = record("first")
    second = record("second")

    first.consume(AgentTextDelta("first-only"))
    second.consume(AgentTextDelta("second-only"))

    assert "second-only" not in repr(first.view(snapshot("first")))
    assert "first-only" not in repr(second.view(snapshot("second")))
