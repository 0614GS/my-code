"""BG-01/BG-02 non-blocking child execution and single notification delivery."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentInvocationSucceeded, AgentTurnInput
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.context.attachments.sources import DerivedAttachmentResolver
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session_cache import SessionContextCache
from my_code.conversation.attachments import BackgroundTaskCompletionAttachment
from my_code.conversation.models import (
    AssistantMessage,
    AttachmentMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.background_tasks.notifications import (
    BackgroundTaskNotificationSource,
)
from my_code.features.background_tasks.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.definitions import build_subagent_definitions
from my_code.features.subagents.models import SubagentParentContext
from my_code.features.subagents.task_tools import TaskWaitTool
from my_code.features.subagents.tool import SubagentTool
from my_code.model.capabilities import (
    ActiveModelEnvironment,
    fallback_descriptor,
    resolve_environment,
)
from my_code.model.events import (
    ModelOutputCompleted,
    ModelStreamEvent,
    ModelStreamSequencer,
    completed_output_payloads,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    InputText,
    ModelOutput,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    PromptStability,
    UserInput,
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
from my_code.tools.base import ToolExecutionContext
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


class BlockingChildModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        output = ModelOutput(
            (ModelTextBlock("background result"),),
            "end_turn",
            TokenUsage(2, 1, provider_reported=True),
        )
        yield ModelStreamSequencer().emit(ModelOutputCompleted(output))


def output(*blocks: ModelTextBlock | ModelToolUseBlock) -> ModelOutput:
    stop_reason = (
        "tool_use"
        if any(isinstance(block, ModelToolUseBlock) for block in blocks)
        else "end_turn"
    )
    return ModelOutput(
        tuple(blocks), stop_reason, TokenUsage(2, 1, provider_reported=True)
    )


def prompt_registry() -> PromptRegistry:
    return PromptRegistry(
        (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
    )


def request_text(request: ModelRequest) -> str:
    return "\n".join(
        block.text
        for item in request.input
        if isinstance(item, UserInput)
        for block in item.content
        if isinstance(block, InputText)
    )


@pytest.mark.asyncio
async def test_background_submit_does_not_wait_and_completion_is_delivered_once(
    tmp_path: Path,
) -> None:
    child_model = BlockingChildModel()
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
    policy = PermissionPolicy(PermissionMode.BYPASS)
    notifications: BackgroundTaskNotificationSource | None = None

    def attachment_resolver() -> DerivedAttachmentResolver:
        return DerivedAttachmentResolver(
            () if notifications is None else (notifications,)
        )

    def build_run(
        spec: AgentRunSpec,
        provider: ProviderClientLease,
        run_environment: ActiveModelEnvironment,
    ) -> AgentRunComponents:
        catalog = spec.tool_catalog
        child_policy = spec.permission_policy
        assert catalog is not None
        assert child_policy is not None
        executor = ToolExecutor(
            catalog.snapshot(),
            child_policy,
            HeadlessPrompter(),
            workspace,
        )
        context = ContextEngine(
            ContextPlanner(
                prompt=spec.prompt_registry or prompt_registry(),
                max_output_tokens=100,
                attachment_resolver=attachment_resolver(),
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
        background_enabled=True,
    )
    notifications = BackgroundTaskNotificationSource(controller.background_registry)
    parent_id = "11111111-1111-1111-1111-111111111111"
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "background"),
        (
            SubagentTool(
                controller,
                parent=SubagentParentContext(parent_id),
                policy=policy,
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
                        "agent_type": "explore",
                        "description": "background work",
                        "prompt": "finish later",
                        "background": True,
                    },
                )
            ),
            output(ModelTextBlock("parent kept going")),
            output(ModelTextBlock("saw completion")),
            output(ModelTextBlock("no duplicate")),
        ]
    )
    parent_executor = ToolExecutor(
        catalog.snapshot(),
        policy,
        HeadlessPrompter(),
        workspace,
    )
    parent_context = ContextEngine(
        ContextPlanner(
            prompt=prompt_registry(),
            max_output_tokens=100,
            attachment_resolver=DerivedAttachmentResolver((notifications,)),
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
    parent_session = Session(tmp_path / "sessions", parent_id)
    parent_runtime = SessionContextCache()

    first_turn = asyncio.create_task(
        parent.submit(
            parent_session, parent_runtime, AgentTurnInput("start background")
        )
    )
    await child_model.entered.wait()
    first = await asyncio.wait_for(first_turn, timeout=1)

    assert isinstance(first, AgentInvocationSucceeded)
    assert first.text == "parent kept going"
    assert child_model.release.is_set() is False
    batch = parent_session.conversation[2]
    assert isinstance(batch, ToolResultBatch)
    started = json.loads(batch.content[0].content)
    assert started["status"] == "started"
    task_id = started["task_id"]
    assert tasks.snapshot(task_id).status is TaskStatus.RUNNING

    child_model.release.set()
    completed = await tasks.wait(task_id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert runs.active_count == 0
    assert leases.active_count == 0

    second_events = [
        event
        async for event in parent.stream_continuation(parent_session, parent_runtime)
    ]
    assert isinstance(second_events[-1], AgentInvocationSucceeded)
    assert "Background task completed" in request_text(parent_model.requests[2])
    assert task_id in request_text(parent_model.requests[2])
    delivered = tuple(
        message
        for message in parent_session.conversation
        if message.kind == "attachment"
    )
    assert len(delivered) == 1
    assert controller.pending_notifications(parent_id) == ()
    assert (
        sum(
            isinstance(message, HumanMessage) for message in parent_session.conversation
        )
        == 1
    )

    third = await parent.submit(
        parent_session, parent_runtime, AgentTurnInput("check again")
    )
    assert isinstance(third, AgentInvocationSucceeded)
    delivered_after_next_turn = tuple(
        message
        for message in parent_session.conversation
        if message.kind == "attachment"
    )
    assert delivered_after_next_turn == delivered

    await tasks.close()
    await runs.close()
    await leases.close()


@pytest.mark.asyncio
async def test_task_wait_commits_result_and_completion_once(tmp_path: Path) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    owner = "55555555-5555-5555-5555-555555555555"
    task_id = "66666666-6666-6666-6666-666666666666"
    registry.register(BackgroundTask(task_id, owner, "subagent", "finished"))
    release = asyncio.Event()

    async def finish() -> None:
        await release.wait()

    await supervisor.submit(
        finish, name="finished", task_id=task_id, on_terminal=registry.terminal
    )
    notifications = BackgroundTaskNotificationSource(registry)
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "wait"),
        (TaskWaitTool(registry, parent=SubagentParentContext(owner)),),
    )
    model = ScriptedModel(
        [
            output(ModelToolUseBlock("wait", "TaskWait", {"task_id": task_id})),
            output(ModelTextBlock("done")),
            output(ModelTextBlock("again")),
        ]
    )
    policy = PermissionPolicy(PermissionMode.BYPASS)
    context = ContextEngine(
        ContextPlanner(
            prompt=prompt_registry(),
            max_output_tokens=100,
            attachment_resolver=DerivedAttachmentResolver((notifications,)),
        ),
        ContextCompactor(model),
    )
    executor = ToolExecutor(
        catalog.snapshot(), policy, HeadlessPrompter(), Workspace(tmp_path)
    )
    agent = AgentEngine(
        model_call=model,
        tool_round=ToolRoundExecutor(executor),
        context=context,
        tool_catalog=catalog,
        max_steps=3,
    )
    session = Session(tmp_path / "sessions", owner)
    runtime = SessionContextCache()

    asyncio.get_running_loop().call_later(0.01, release.set)
    await agent.submit(session, runtime, AgentTurnInput("wait"))
    assert len(model.requests) == 2
    batch = next(
        item for item in session.conversation if isinstance(item, ToolResultBatch)
    )
    result = json.loads(batch.content[0].content)
    assert result["end_reason"] == "completed"
    attachments = [
        item
        for item in session.conversation
        if isinstance(item, AttachmentMessage)
        and isinstance(item.payload, BackgroundTaskCompletionAttachment)
    ]
    assert len(attachments) == 1
    assert attachments[0].parent_uuid == batch.uuid
    assert registry.pending(owner) == ()
    await agent.submit(session, runtime, AgentTurnInput("again"))
    assert (
        len(
            [
                item
                for item in session.conversation
                if isinstance(item, AttachmentMessage)
                and isinstance(item.payload, BackgroundTaskCompletionAttachment)
            ]
        )
        == 1
    )
    await supervisor.close()


@pytest.mark.asyncio
async def test_failed_tool_round_commit_keeps_completion_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = TaskSupervisor()
    registry = BackgroundTaskRegistry(supervisor)
    owner = "77777777-7777-7777-7777-777777777777"
    task_id = "88888888-8888-8888-8888-888888888888"
    registry.register(BackgroundTask(task_id, owner, "subagent", "finished"))

    async def finish() -> None:
        return None

    await supervisor.submit(
        finish, name="finished", task_id=task_id, on_terminal=registry.terminal
    )
    await supervisor.wait(task_id)
    wait_tool = TaskWaitTool(registry, parent=SubagentParentContext(owner))
    result = await wait_tool.execute(
        {"task_id": task_id}, ToolExecutionContext(tmp_path, session_id=owner)
    )
    assert len(result.new_attachments) == 1
    session = Session(tmp_path / "sessions", owner)
    human = HumanMessage("wait")
    session.append_human_message(human)
    assistant = AssistantMessage(
        (ToolCall("wait", "TaskWait", {"task_id": task_id}),),
        TokenUsage(),
        parent_uuid=human.uuid,
    )
    session.append_assistant_message(assistant)
    batch = ToolResultBatch(
        (ToolResult("wait", result.content, ToolResultPresentation("Task completed")),),
        assistant.uuid,
        parent_uuid=assistant.uuid,
    )

    def fail(_: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(session._store, "_append_records", fail)  # type: ignore[attr-defined]
        with pytest.raises(OSError, match="disk full"):
            session.commit_tool_round(batch, result.new_attachments)
    assert len(registry.pending(owner)) == 1
    session.commit_tool_round(batch, result.new_attachments)
    BackgroundTaskNotificationSource(registry).acknowledge(result.new_attachments)
    assert registry.pending(owner) == ()
    await supervisor.close()
