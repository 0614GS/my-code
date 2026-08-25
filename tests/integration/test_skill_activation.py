"""SKILL-02: lazy activation and immutable per-step capability snapshots."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.window import ContextWindow
from my_code.model.events import (
    ModelStreamEvent,
    ModelStreamSequencer,
    completed_output_payloads,
)
from my_code.model.primitives import JsonObject
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelToolDefinition,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.sessions.session import Session
from my_code.skills.attachments import SkillListingAttachmentSource
from my_code.skills.discovery import SkillSearchRoot
from my_code.skills.models import SkillSourceId, SkillSourceKind
from my_code.skills.runtime import SkillRuntime
from my_code.tools.base import Tool, ToolContext, ToolOutput
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class SafeTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.executions = 0

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            self.name,
            f"{self.name} description",
            {"type": "object", "additionalProperties": False},
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="test tool",
            reason=PermissionDecisionReason(PermissionDecisionKind.TOOL, "test"),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        if tool_input:
            raise ValueError(f"{self.name} expects no input")

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del tool_input, context
        self.executions += 1
        return ToolOutput(f"{self.name} completed")


class ScriptedModel:
    def __init__(
        self,
        outputs: tuple[ModelOutput, ...],
        on_request: Callable[[int], None] | None = None,
    ) -> None:
        self.outputs = outputs
        self.on_request = on_request
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        index = len(self.requests)
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(index)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(self.outputs[index]):
            yield sequencer.emit(payload)


class ApprovingPrompter:
    async def confirm(self, request):
        del request
        return PermissionConfirmation(True)


def _write_skill(
    root: Path,
    name: str,
    *,
    body: str,
    allowed_tools: str | None = None,
) -> None:
    target = root / name
    target.mkdir(parents=True)
    allowlist = f"allowed-tools: {allowed_tools}\n" if allowed_tools is not None else ""
    (target / "SKILL.md").write_text(
        f"---\ndescription: {name} metadata\n{allowlist}---\n{body}\n",
        encoding="utf-8",
    )


async def _engine(
    tmp_path: Path,
    model: ScriptedModel,
) -> tuple[AgentEngine, Session, SkillRuntime, SafeTool, SafeTool]:
    tools = ToolCatalog()
    echo = SafeTool("Echo")
    other = SafeTool("Other")
    tools.register_source(ToolSourceId("test", "safe"), (echo, other))
    runtime = SkillRuntime(
        enabled=True,
        roots=(
            SkillSearchRoot(
                SkillSourceId(300, SkillSourceKind.PROJECT, "workspace"),
                tmp_path / "skills",
            ),
        ),
        tool_catalog=tools,
    )
    await runtime.start()
    executor = ToolExecutor(
        tools.snapshot(),
        PermissionPolicy(PermissionMode.BYPASS),
        ApprovingPrompter(),
        Workspace(tmp_path),
    )
    context = ContextEngine(
        ContextPlanner(
            window=ContextWindow(20_000),
            prompt=PromptRegistry(
                (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
            ),
            max_output_tokens=100,
            attachment_resolver=DerivedAttachmentResolver(
                (SkillListingAttachmentSource(runtime.catalog),)
            ),
        ),
        ContextCompactor(model),
    )
    session = Session(
        tmp_path / "sessions",
        "11111111-1111-1111-1111-111111111111",
    )
    return (
        AgentEngine(
            model_call=model,
            tool_round=ToolRoundExecutor(executor),
            context=context,
            tool_catalog=tools,
            max_steps=4,
        ),
        session,
        runtime,
        echo,
        other,
    )


@pytest.mark.asyncio
async def test_activation_adds_durable_body_without_narrowing_tools(
    tmp_path: Path,
) -> None:
    body = "PRIVATE ACTIVATED INSTRUCTION"
    _write_skill(
        tmp_path / "skills",
        "focused",
        body=body,
        allowed_tools="[Echo]",
    )
    model = ScriptedModel(
        (
            ModelOutput(
                (ModelToolUseBlock("activate", "Skill", {"skill": "focused"}),),
                "tool_use",
            ),
            ModelOutput(
                (ModelToolUseBlock("echo", "Echo", {}),),
                "tool_use",
            ),
            ModelOutput((ModelTextBlock("finished"),), "end_turn"),
        )
    )
    engine, session, runtime, echo, other = await _engine(tmp_path, model)

    outcome = await engine.submit(session, AgentTurnInput("use a focused skill"))

    assert isinstance(outcome, AgentTurnSucceeded)
    assert body not in model.requests[0].system_prompt.text
    assert body not in model.requests[1].system_prompt.text
    assert any(body in str(item) for item in model.requests[1].input)
    assert any(body in str(item) for item in model.requests[2].input)
    assert {tool.name for tool in model.requests[1].tools} == {
        "Echo",
        "Other",
        "Skill",
    }
    assert echo.executions == 1
    assert other.executions == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_reload_during_request_only_changes_next_step(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", body="alpha instructions")
    runtime_ref: list[SkillRuntime] = []

    def reload_on_first_request(index: int) -> None:
        if index != 0:
            return
        _write_skill(skills, "beta", body="beta instructions")
        runtime_ref[0].reload()

    model = ScriptedModel(
        (
            ModelOutput(
                (ModelToolUseBlock("echo", "Echo", {}),),
                "tool_use",
            ),
            ModelOutput((ModelTextBlock("finished"),), "end_turn"),
        ),
        on_request=reload_on_first_request,
    )
    engine, session, runtime, echo, _ = await _engine(tmp_path, model)
    runtime_ref.append(runtime)

    await engine.submit(session, AgentTurnInput("reload safely"))

    first_skill = next(tool for tool in model.requests[0].tools if tool.name == "Skill")
    second_skill = next(
        tool for tool in model.requests[1].tools if tool.name == "Skill"
    )
    assert first_skill.input_schema == second_skill.input_schema
    assert "enum" not in first_skill.input_schema["properties"]["skill"]  # type: ignore[index]
    assert "alpha" in str(model.requests[0].input)
    assert "beta" in str(model.requests[1].input)
    assert echo.executions == 1
    await runtime.close()
