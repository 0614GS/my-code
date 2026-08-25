"""SNAP-01: one Agent step plans and executes one ToolCatalog version."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextRuntime
from my_code.context.window import ContextWindow
from my_code.conversation.models import ToolResultBatch
from my_code.model.events import (
    ModelStreamEvent,
    ModelStreamSequencer,
    completed_output_payloads,
)
from my_code.model.primitives import JsonObject, TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelToolDefinition,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.sessions.session import Session
from my_code.tools.base import Tool, ToolContext, ToolOutput
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class VersionedTool(Tool):
    def __init__(self, label: str) -> None:
        self.label = label
        self.executions = 0

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name="Dynamic",
            description=f"{self.label} definition",
            input_schema={"type": "object", "additionalProperties": False},
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    def validate_input(self, tool_input: JsonObject) -> None:
        if tool_input:
            raise ValueError("Dynamic expects no input")

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Read-only test tool.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "snapshot-test"
            ),
        )

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        del tool_input, context
        self.executions += 1
        return ToolOutput(f"executed:{self.label}")


class ReplacingModel:
    def __init__(
        self,
        catalog: ToolCatalog,
        source: ToolSourceId,
        replacement: Tool,
    ) -> None:
        self.catalog = catalog
        self.source = source
        self.replacement = replacement
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.catalog.replace_source(self.source, (self.replacement,))
            output = ModelOutput(
                (ModelToolUseBlock("dynamic-1", "Dynamic", {}),),
                "tool_use",
                TokenUsage(2, 1),
            )
        else:
            output = ModelOutput(
                (ModelTextBlock("finished"),),
                "end_turn",
                TokenUsage(3, 1),
            )
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


@pytest.mark.asyncio
async def test_catalog_update_during_step_waits_until_next_step(
    tmp_path: Path,
) -> None:
    source = ToolSourceId("test", "dynamic")
    original = VersionedTool("original")
    replacement = VersionedTool("replacement")
    catalog = ToolCatalog()
    catalog.register_source(source, (original,))
    initial_snapshot = catalog.snapshot()
    model = ReplacingModel(catalog, source, replacement)
    executor = ToolExecutor(
        tools=initial_snapshot,
        policy=PermissionPolicy(PermissionMode.BYPASS),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )
    context = ContextEngine(
        ContextPlanner(
            window=ContextWindow(10_000),
            prompt=PromptRegistry(
                (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
            ),
            max_output_tokens=100,
        ),
        ContextCompactor(model),
    )
    session = Session(
        tmp_path / "sessions",
        "11111111-1111-1111-1111-111111111111",
    )
    engine = AgentEngine(
        model_call=model,
        tool_round=ToolRoundExecutor(executor),
        context=context,
        tool_catalog=catalog,
        max_steps=3,
    )

    outcome = await engine.submit(
        session, ContextRuntime(), AgentTurnInput("use Dynamic")
    )

    assert outcome == AgentTurnSucceeded("finished", 2, TokenUsage(5, 2))
    assert original.executions == 1
    assert replacement.executions == 0
    assert model.requests[0].tools[0].description == "original definition"
    assert model.requests[1].tools[0].description == "replacement definition"
    result_batch = session.conversation[2]
    assert isinstance(result_batch, ToolResultBatch)
    assert result_batch.content[0].content == "executed:original"
