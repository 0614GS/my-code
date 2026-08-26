"""SUB-01 foreground child lifecycle and transcript isolation."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextRuntime
from my_code.context.user_context import AgentsUserContextResolver
from my_code.context.window import ContextWindow
from my_code.conversation.models import HumanMessage, ToolResultBatch
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.definitions import build_subagent_definitions
from my_code.features.subagents.models import SubagentParentContext
from my_code.features.subagents.tool import SubagentTool
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
    ModelToolUseBlock,
    PromptStability,
)
from my_code.permissions.models import PermissionMode
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
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class ScriptedModel:
    def __init__(self, outputs: list[ModelOutput]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        output = self.outputs.pop(0)
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


def output(*blocks: ModelTextBlock | ModelToolUseBlock) -> ModelOutput:
    stop_reason = (
        "tool_use"
        if any(isinstance(block, ModelToolUseBlock) for block in blocks)
        else "end_turn"
    )
    return ModelOutput(tuple(blocks), stop_reason, TokenUsage(2, 1))


def prompt_registry() -> PromptRegistry:
    return PromptRegistry(
        (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
    )


@pytest.mark.asyncio
async def test_foreground_subagent_uses_child_session_and_returns_one_result(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "child-only repository instructions",
        encoding="utf-8",
    )
    child_model = ScriptedModel([output(ModelTextBlock("child answer"))])
    connection = ProviderConnection(
        id="test",
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        model="test-model",
        base_url=None,
        api_key=None,
        credential_source=CredentialSource.NONE,
    )
    leases = ProviderLeaseRegistry(connection, factory=lambda _: child_model)
    environment = resolve_environment(
        fallback_descriptor("test-model"),
        requested_output_tokens=100,
        configured_trigger_tokens=None,
    )
    workspace = Workspace(tmp_path)
    parent_policy = PermissionPolicy(PermissionMode.BYPASS)

    def build_run(
        spec: AgentRunSpec,
        provider: ProviderClientLease,
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
                window=ContextWindow(10_000),
                prompt=spec.prompt_registry or prompt_registry(),
                max_output_tokens=100,
                user_context_resolver=AgentsUserContextResolver(tmp_path),
                binding_resolver=lambda: provider.binding,
                model_environment=lambda: run_environment,
            ),
            ContextCompactor(provider),
        )
        return AgentRunComponents(
            AgentEngine(
                model_call=provider,
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
    controller = SubagentController(
        runs=runs,
        tasks=tasks,
        project_state_dir=tmp_path / "sessions",
        definitions=build_subagent_definitions(tmp_path),
    )
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "subagent"),
        (
            SubagentTool(
                controller,
                parent=SubagentParentContext("11111111-1111-1111-1111-111111111111"),
                policy=parent_policy,
            ),
        ),
    )
    parent_model = ScriptedModel(
        [
            output(
                ModelToolUseBlock(
                    "subagent-1",
                    "Subagent",
                    {
                        "agent_type": "general",
                        "description": "answer child",
                        "prompt": "answer independently",
                    },
                )
            ),
            output(ModelTextBlock("parent final")),
        ]
    )
    parent_executor = ToolExecutor(
        catalog.snapshot(),
        parent_policy,
        HeadlessPrompter(),
        workspace,
    )
    parent_context = ContextEngine(
        ContextPlanner(
            window=ContextWindow(10_000),
            prompt=prompt_registry(),
            max_output_tokens=100,
        ),
        ContextCompactor(parent_model),
    )
    parent = AgentEngine(
        model_call=parent_model,
        tool_round=ToolRoundExecutor(parent_executor),
        context=parent_context,
        tool_catalog=catalog,
        max_steps=3,
    )
    parent_session = Session(
        tmp_path / "sessions",
        "11111111-1111-1111-1111-111111111111",
    )

    result = await parent.submit(
        parent_session, ContextRuntime(), AgentTurnInput("delegate this")
    )

    assert isinstance(result, AgentTurnSucceeded)
    assert result.text == "parent final"
    parent_history = parent_session.conversation
    assert [entry.kind for entry in parent_history] == [
        "human",
        "assistant",
        "tool_result_batch",
        "assistant",
    ]
    batch = parent_history[2]
    assert isinstance(batch, ToolResultBatch)
    assert len(batch.content) == 1
    payload = json.loads(batch.content[0].content)
    assert payload["status"] == "succeeded"
    assert payload["result"] == "child answer"
    child_session = Session(tmp_path / "sessions", payload["run_id"])
    child_history = child_session.conversation
    assert [entry.kind for entry in child_history] == ["human", "assistant"]
    assert isinstance(child_history[0], HumanMessage)
    assert child_history[0].content == "answer independently"
    assert "delegate this" not in str(child_history)
    child_request = child_model.requests[0]
    assert "isolated coding agent" in child_request.system_prompt.text
    assert "child-only repository instructions" in str(child_request.input)
    assert tasks.snapshots()[0].status is TaskStatus.SUCCEEDED
    assert runs.active_count == 0
    assert leases.active_count == 0

    await tasks.close()
    await runs.close()
    await leases.close()
