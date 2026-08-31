"""Offline cross-component characterization of the M0 foreground runtime."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextRuntime
from my_code.conversation.models import AssistantMessage, ToolResultBatch
from my_code.model.events import (
    ModelStreamEvent,
    ModelStreamSequencer,
    completed_output_payloads,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.sessions.session import Session
from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class ScriptedModel:
    def __init__(self, outputs: list[ModelOutput]) -> None:
        self._outputs = outputs
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        output = self._outputs.pop(0)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


def build_runtime(
    tmp_path: Path,
    model: ScriptedModel,
) -> tuple[AgentEngine, Session, ToolCatalog]:
    catalog = ToolCatalog()
    catalog.register_source(ToolSourceId("test", "integration"), builtin_tools())
    tools = catalog.snapshot()
    executor = ToolExecutor(
        tools=tools,
        policy=PermissionPolicy(PermissionMode.BYPASS),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )
    context = ContextEngine(planner, ContextCompactor(model))
    session = Session(
        tmp_path / "sessions",
        "11111111-1111-1111-1111-111111111111",
    )
    return (
        AgentEngine(
            model_call=model,
            tool_round=ToolRoundExecutor(executor),
            context=context,
            tool_catalog=catalog,
            max_steps=3,
        ),
        session,
        catalog,
    )


@pytest.mark.asyncio
async def test_foreground_turn_persists_closed_tool_round_before_next_step(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.txt").write_text("hello from tool", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelOutput(
                (ModelToolUseBlock("read-1", "Read", {"path": "hello.txt"}),),
                "tool_use",
                TokenUsage(3, 1, provider_reported=True),
            ),
            ModelOutput(
                (ModelTextBlock("finished"),),
                "end_turn",
                TokenUsage(5, 1, provider_reported=True),
            ),
        ]
    )
    engine, session, catalog = build_runtime(tmp_path, model)

    outcome = await engine.submit(
        session, ContextRuntime(), AgentTurnInput("read hello.txt")
    )

    assert outcome == AgentTurnSucceeded(
        "finished", 2, TokenUsage(8, 2, provider_reported=True)
    )
    assert len(model.requests) == 2
    assert model.requests[0].tools == catalog.snapshot().definitions
    assert model.requests[1].tools == catalog.snapshot().definitions
    history = session.conversation
    assert [entry.kind for entry in history] == [
        "human",
        "assistant",
        "tool_result_batch",
        "assistant",
    ]
    assert isinstance(history[1], AssistantMessage)
    assert isinstance(history[2], ToolResultBatch)
    assert history[2].source_assistant_id == history[1].uuid
    assert history[2].content[0].tool_use_id == "read-1"
    assert "hello from tool" in history[2].content[0].content

    restored = Session(
        tmp_path / "sessions",
        "11111111-1111-1111-1111-111111111111",
    )
    assert restored.conversation == history
