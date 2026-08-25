"""Subagent budget, hierarchy, and cancellation policy tests."""

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from my_code.agent.models import AgentTurnInput, AgentTurnSucceeded
from my_code.application.runs import AgentRun, AgentRunFactory, AgentRunSpec
from my_code.conversation.models import ToolCall
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.models import (
    SubagentLimits,
    SubagentParentContext,
    SubagentSpec,
)
from my_code.features.subagents.task_tools import (
    TaskCancelTool,
    TaskListTool,
    TaskOutputTool,
)
from my_code.features.subagents.tool import SubagentTool
from my_code.model.primitives import TokenUsage
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import ToolExecutionError
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


class ControlledRun:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release
        self.closed = False
        self.turn_input: AgentTurnInput | None = None

    async def submit(self, turn_input: AgentTurnInput) -> AgentTurnSucceeded:
        self.turn_input = turn_input
        self.entered.set()
        await self.release.wait()
        return AgentTurnSucceeded("done", 1, TokenUsage(1, 1))

    async def close(self) -> None:
        self.closed = True


class ControlledRunFactory:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.specs: list[AgentRunSpec] = []
        self.runs: list[ControlledRun] = []

    async def create(self, spec: AgentRunSpec) -> AgentRun:
        self.specs.append(spec)
        run = ControlledRun(self.entered, self.release)
        self.runs.append(run)
        return cast(AgentRun, run)


class FailingRun(ControlledRun):
    async def submit(self, turn_input: AgentTurnInput) -> AgentTurnSucceeded:
        self.turn_input = turn_input
        self.entered.set()
        raise RuntimeError("child failed")


class FailingRunFactory(ControlledRunFactory):
    async def create(self, spec: AgentRunSpec) -> AgentRun:
        self.specs.append(spec)
        run = FailingRun(self.entered, self.release)
        self.runs.append(run)
        return cast(AgentRun, run)


def build_controller(
    tmp_path: Path,
    *,
    limits: SubagentLimits | None = None,
    background_enabled: bool = False,
) -> tuple[SubagentController, ControlledRunFactory, TaskSupervisor]:
    factory = ControlledRunFactory()
    tasks = TaskSupervisor()
    controller = SubagentController(
        runs=cast(AgentRunFactory, factory),
        tasks=tasks,
        project_state_dir=tmp_path / "sessions",
        limits=limits,
        background_enabled=background_enabled,
    )
    return controller, factory, tasks


def parent(depth: int = 0) -> SubagentParentContext:
    return SubagentParentContext(
        "11111111-1111-1111-1111-111111111111",
        depth,
    )


@pytest.mark.asyncio
async def test_foreground_wait_cancellation_cancels_and_closes_child(
    tmp_path: Path,
) -> None:
    controller, factory, tasks = build_controller(tmp_path)
    foreground = asyncio.create_task(
        controller.run_foreground(
            SubagentSpec("work", "cancel test"),
            parent=parent(),
            parent_policy=PermissionPolicy(PermissionMode.BYPASS),
            available_tools={},
            tool_snapshot_version=1,
        )
    )
    await factory.entered.wait()

    foreground.cancel()
    with pytest.raises(asyncio.CancelledError):
        await foreground

    assert tasks.snapshots()[0].status is TaskStatus.CANCELLED
    assert tasks.snapshots()[0].failure is not None
    assert factory.runs[0].closed is True
    assert controller.active_children(parent().run_id) == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_subagent_timeout_is_a_closed_cancelled_terminal(
    tmp_path: Path,
) -> None:
    controller, factory, tasks = build_controller(
        tmp_path,
        limits=SubagentLimits(timeout_seconds=0.01),
    )

    completed = await controller.run_foreground(
        SubagentSpec("work", "timeout test"),
        parent=parent(),
        parent_policy=PermissionPolicy(PermissionMode.BYPASS),
        available_tools={},
        tool_snapshot_version=1,
    )

    assert completed.task.status is TaskStatus.CANCELLED
    assert completed.task.failure is not None
    assert completed.task.failure.kind == "timeout"
    assert factory.runs[0].closed is True
    assert controller.active_children(parent().run_id) == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_active_child_and_depth_limits_are_enforced_before_spawn(
    tmp_path: Path,
) -> None:
    controller, factory, tasks = build_controller(
        tmp_path,
        limits=SubagentLimits(max_depth=1, max_active_children=1),
    )
    started, handle = await controller.start(
        SubagentSpec("work", "first"),
        parent=parent(),
        parent_policy=PermissionPolicy(PermissionMode.BYPASS),
        available_tools={},
        tool_snapshot_version=1,
    )
    await factory.entered.wait()

    with pytest.raises(ToolExecutionError, match="active child limit"):
        await controller.start(
            SubagentSpec("work", "second"),
            parent=parent(),
            parent_policy=PermissionPolicy(PermissionMode.BYPASS),
            available_tools={},
            tool_snapshot_version=1,
        )
    with pytest.raises(ToolExecutionError, match="nesting depth"):
        await controller.start(
            SubagentSpec("work", "nested"),
            parent=parent(depth=1),
            parent_policy=PermissionPolicy(PermissionMode.BYPASS),
            available_tools={},
            tool_snapshot_version=1,
        )

    assert controller.active_children(parent().run_id) == 1
    assert handle.task_id == started.task_id
    await handle.cancel()
    assert controller.active_children(parent().run_id) == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_child_run_spec_captures_step_budget_and_disables_permission_updates(
    tmp_path: Path,
) -> None:
    limits = SubagentLimits(max_steps=7, max_tokens=900)
    controller, factory, tasks = build_controller(tmp_path, limits=limits)
    started, handle = await controller.start(
        SubagentSpec("explicit prompt", "budget test"),
        parent=parent(),
        parent_policy=PermissionPolicy(PermissionMode.DEFAULT),
        available_tools={},
        tool_snapshot_version=9,
    )
    await factory.entered.wait()

    spec = factory.specs[0]
    assert spec.run_id == started.run_id
    assert spec.parent_run_id == parent().run_id
    assert spec.max_steps == 7
    assert spec.max_tokens == 900
    assert spec.allow_permission_updates is False
    assert spec.permission_policy is not None
    assert spec.permission_policy is not PermissionPolicy(PermissionMode.DEFAULT)
    assert spec.permission_policy.mode is PermissionMode.DEFAULT
    assert spec.tool_catalog is not None
    assert spec.tool_catalog.snapshot().version == 1
    assert factory.runs[0].turn_input == AgentTurnInput("explicit prompt")

    factory.release.set()
    completed = await handle.wait()
    assert completed.status is TaskStatus.SUCCEEDED
    await tasks.close()


@pytest.mark.asyncio
async def test_child_failure_closes_parent_tool_result_and_run(tmp_path: Path) -> None:
    factory = FailingRunFactory()
    tasks = TaskSupervisor()
    controller = SubagentController(
        runs=cast(AgentRunFactory, factory),
        tasks=tasks,
        project_state_dir=tmp_path / "sessions",
    )
    policy = PermissionPolicy(PermissionMode.BYPASS)
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "subagent"),
        (SubagentTool(controller, parent=parent(), policy=policy),),
    )
    executor = ToolExecutor(
        catalog.snapshot(),
        policy,
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall(
            "subagent-1",
            "Subagent",
            {"description": "failure", "prompt": "fail now"},
        ),
        tools=catalog.snapshot(),
    )

    assert outcome.result.is_error is True
    assert '"status": "failed"' in outcome.result.content
    assert "child failed" in outcome.result.content
    assert tasks.snapshots()[0].status is TaskStatus.FAILED
    assert factory.runs[0].closed is True
    assert controller.active_children(parent().run_id) == 0
    await tasks.close()


@pytest.mark.asyncio
async def test_background_task_tools_are_owner_scoped_and_cancel_child(
    tmp_path: Path,
) -> None:
    controller, factory, tasks = build_controller(
        tmp_path,
        background_enabled=True,
    )
    owner = parent()
    started, _ = await controller.start(
        SubagentSpec("work", "background tools"),
        parent=owner,
        parent_policy=PermissionPolicy(PermissionMode.BYPASS),
        available_tools={},
        tool_snapshot_version=1,
        background=True,
    )
    await factory.entered.wait()
    policy = PermissionPolicy(PermissionMode.BYPASS)
    catalog = ToolCatalog()
    catalog.register_source(
        ToolSourceId("test", "task-tools"),
        (
            TaskListTool(controller, parent=owner),
            TaskOutputTool(controller, parent=owner),
            TaskCancelTool(controller, parent=owner),
        ),
    )
    executor = ToolExecutor(
        catalog.snapshot(),
        policy,
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    listed = await executor.execute(
        ToolCall("list", "TaskList", {}),
        tools=catalog.snapshot(),
        run_id=owner.run_id,
    )
    listed_payload = json.loads(listed.result.content)
    assert listed_payload["tasks"][0]["task_id"] == started.task_id
    assert listed_payload["tasks"][0]["status"] == "running"

    hidden = await executor.execute(
        ToolCall("hidden", "TaskOutput", {"task_id": started.task_id}),
        tools=catalog.snapshot(),
        run_id="22222222-2222-2222-2222-222222222222",
    )
    assert hidden.result.is_error is True
    assert "Unknown background task" in hidden.result.content

    cancelled = await executor.execute(
        ToolCall("cancel", "TaskCancel", {"task_id": started.task_id}),
        tools=catalog.snapshot(),
        run_id=owner.run_id,
    )
    cancelled_payload = json.loads(cancelled.result.content)
    assert cancelled_payload["status"] == "cancelled"
    assert factory.runs[0].closed is True
    assert (
        controller.pending_notifications(owner.run_id)[0].task.task_id
        == started.task_id
    )
    await tasks.close()


@pytest.mark.asyncio
async def test_cancelling_parent_task_cancels_background_child_tree(
    tmp_path: Path,
) -> None:
    controller, factory, tasks = build_controller(
        tmp_path,
        background_enabled=True,
    )
    root_task_id = "root-task"

    async def run_parent() -> object:
        await controller.start(
            SubagentSpec("work", "tree child"),
            parent=SubagentParentContext(
                parent().run_id,
                task_id=root_task_id,
            ),
            parent_policy=PermissionPolicy(PermissionMode.BYPASS),
            available_tools={},
            tool_snapshot_version=1,
            background=True,
        )
        await asyncio.Event().wait()

    root = await tasks.submit(
        run_parent,
        name="parent",
        task_id=root_task_id,
    )
    await factory.entered.wait()

    cancelled = await root.cancel("Parent was cancelled.")

    assert cancelled.status is TaskStatus.CANCELLED
    child = next(
        item for item in tasks.snapshots() if item.parent_task_id == root_task_id
    )
    assert child.status is TaskStatus.CANCELLED
    assert factory.runs[0].closed is True
    assert controller.active_children(parent().run_id) == 0
    await tasks.close()
