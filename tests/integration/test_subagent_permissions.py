"""SUB-02 child tool and permission capabilities can only narrow."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.conversation.models import ToolResultBatch
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.definitions import build_subagent_definitions
from my_code.features.subagents.models import (
    SubagentParentContext,
    SubagentSpec,
    SubagentType,
)
from my_code.foundation.json import JsonObject
from my_code.model.capabilities import (
    ActiveModelEnvironment,
    fallback_descriptor,
    resolve_environment,
)
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
    ModelToolDefinition,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.providers.leases import ProviderClientLease, ProviderLeaseRegistry
from my_code.providers.router import ProviderConnection
from my_code.runtime.runs import (
    AgentRunComponents,
    AgentRunFactory,
    AgentRunSpec,
)
from my_code.sessions.session import Session
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolOutput,
)
from my_code.tools.builtin.write_file import WriteFileTool
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class ScriptedProvider:
    def __init__(self, outputs: list[ModelOutput]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        output = self.outputs.pop(0)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


class ProbeTool(Tool):
    def __init__(self) -> None:
        self.executions = 0

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "Probe",
            "Record whether child permission checks were bypassed.",
            {"type": "object", "additionalProperties": False},
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Probe permits execution locally.",
            reason=PermissionDecisionReason(PermissionDecisionKind.TOOL, "probe"),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        if tool_input:
            raise ValueError("Probe accepts no input")

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        del tool_input, context
        self.executions += 1
        return ToolOutput("executed")


def output(*blocks: ModelTextBlock | ModelToolUseBlock) -> ModelOutput:
    stop_reason = (
        "tool_use"
        if any(isinstance(block, ModelToolUseBlock) for block in blocks)
        else "end_turn"
    )
    return ModelOutput(
        tuple(blocks), stop_reason, TokenUsage(2, 1, provider_reported=True)
    )


def build_controller(
    tmp_path: Path,
    provider: ScriptedProvider,
) -> tuple[SubagentController, TaskSupervisor, AgentRunFactory, ProviderLeaseRegistry]:
    connection = ProviderConnection(
        id="test",
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        model="test-model",
        base_url=None,
        api_key=None,
        credential_source=CredentialSource.NONE,
    )
    leases = ProviderLeaseRegistry(connection, factory=lambda _: provider)
    environment = resolve_environment(
        fallback_descriptor("test-model"),
        requested_output_tokens=100,
        configured_trigger_tokens=None,
    )
    workspace = Workspace(tmp_path)

    def build_run(
        spec: AgentRunSpec,
        lease: ProviderClientLease,
        run_environment: ActiveModelEnvironment,
    ) -> AgentRunComponents:
        catalog = spec.tool_catalog
        policy = spec.permission_policy
        assert catalog is not None
        assert policy is not None
        executor = ToolExecutor(
            catalog.snapshot(),
            policy,
            HeadlessPrompter(),
            workspace,
        )
        context = ContextEngine(
            ContextPlanner(
                prompt=spec.prompt_registry
                or PromptRegistry(
                    (
                        PromptSection(
                            "core",
                            PromptStability.STATIC,
                            lambda: "system",
                        ),
                    )
                ),
                max_output_tokens=100,
                binding_resolver=lambda: lease.binding,
                model_environment=lambda: run_environment,
            ),
            ContextCompactor(lease),
        )
        return AgentRunComponents(
            AgentEngine(
                model_call=lease,
                tool_round=ToolRoundExecutor(executor),
                context=context,
                tool_catalog=catalog,
                max_steps=spec.max_steps,
            ),
            context,
            executor,
        )

    runs = AgentRunFactory(leases, lambda: environment, build_run)
    tasks = TaskSupervisor()
    return (
        SubagentController(
            runs=runs,
            tasks=tasks,
            project_state_dir=tmp_path / "sessions",
            definitions=build_subagent_definitions(tmp_path),
        ),
        tasks,
        runs,
        leases,
    )


async def close_runtime(
    tasks: TaskSupervisor,
    runs: AgentRunFactory,
    leases: ProviderLeaseRegistry,
) -> None:
    await tasks.close()
    await runs.close()
    await leases.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", [PermissionBehavior.ASK, PermissionBehavior.DENY])
async def test_child_cannot_promote_parent_ask_or_deny(
    tmp_path: Path,
    behavior: PermissionBehavior,
) -> None:
    provider = ScriptedProvider(
        [
            output(ModelToolUseBlock("probe-1", "Probe", {})),
            output(ModelTextBlock("handled denial")),
        ]
    )
    controller, tasks, runs, leases = build_controller(tmp_path, provider)
    probe = ProbeTool()
    parent_policy = PermissionPolicy(
        PermissionMode.BYPASS,
        (PermissionRule("Probe", behavior),),
    )

    completed = await controller.run_foreground(
        SubagentSpec(SubagentType.GENERAL, "try probe", "permission test"),
        parent=SubagentParentContext("11111111-1111-1111-1111-111111111111"),
        parent_policy=parent_policy,
        available_tools={"Probe": probe},
        tool_snapshot_version=7,
    )

    assert completed.outcome is not None
    assert probe.executions == 0
    assert [definition.name for definition in provider.requests[0].tools] == ["Probe"]
    history = Session(tmp_path / "sessions", completed.run_id).conversation
    result_batch = history[2]
    assert isinstance(result_batch, ToolResultBatch)
    assert result_batch.content[0].is_error is True
    assert "Permission denied" in result_batch.content[0].content
    await close_runtime(tasks, runs, leases)


@pytest.mark.asyncio
async def test_explore_intersects_fixed_tools_with_spawn_snapshot(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([output(ModelTextBlock("unused"))])
    controller, tasks, runs, leases = build_controller(tmp_path, provider)
    probe = ProbeTool()

    started, handle = await controller.start(
        SubagentSpec(SubagentType.EXPLORE, "inspect", "intersection test"),
        parent=SubagentParentContext("11111111-1111-1111-1111-111111111111"),
        parent_policy=PermissionPolicy(PermissionMode.BYPASS),
        available_tools={"Probe": probe},
        tool_snapshot_version=4,
    )
    snapshot = await handle.wait()

    assert snapshot.status.value == "succeeded"
    assert started.agent_type is SubagentType.EXPLORE
    assert provider.requests[0].tools == ()
    await close_runtime(tasks, runs, leases)


@pytest.mark.asyncio
async def test_child_cannot_escape_parent_workspace_in_bypass_mode(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            output(
                ModelToolUseBlock(
                    "write-1",
                    "Write",
                    {"path": "../escaped.txt", "content": "forbidden"},
                )
            ),
            output(ModelTextBlock("handled boundary")),
        ]
    )
    controller, tasks, runs, leases = build_controller(tmp_path, provider)

    completed = await controller.run_foreground(
        SubagentSpec(SubagentType.GENERAL, "try escape", "workspace test"),
        parent=SubagentParentContext("11111111-1111-1111-1111-111111111111"),
        parent_policy=PermissionPolicy(PermissionMode.BYPASS),
        available_tools={"Write": WriteFileTool()},
        tool_snapshot_version=5,
    )

    assert completed.outcome is not None
    assert not (tmp_path.parent / "escaped.txt").exists()
    history = Session(tmp_path / "sessions", completed.run_id).conversation
    result_batch = history[2]
    assert isinstance(result_batch, ToolResultBatch)
    assert result_batch.content[0].is_error is True
    assert "Permission denied" in result_batch.content[0].content
    await close_runtime(tasks, runs, leases)
