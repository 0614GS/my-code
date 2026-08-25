"""Create isolated child Sessions/Runs and supervise their lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from my_code.application.runs import AgentRunFactory, AgentRunSpec
from my_code.features.subagents.models import (
    BackgroundSubagent,
    CompletedSubagent,
    StartedSubagent,
    SubagentLimits,
    SubagentParentContext,
    SubagentSpec,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.sessions.session import Session
from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskHandle, TaskSupervisor
from my_code.tools.base import Tool, ToolExecutionError
from my_code.tools.catalog import ToolCatalog, ToolSourceId


class SubagentController:
    """Product policy over AgentRunFactory and generic TaskSupervisor."""

    def __init__(
        self,
        *,
        runs: AgentRunFactory,
        tasks: TaskSupervisor,
        project_state_dir: Path,
        limits: SubagentLimits | None = None,
        background_enabled: bool = False,
    ) -> None:
        self.runs = runs
        self.tasks = tasks
        self.project_state_dir = project_state_dir
        self.limits = limits or SubagentLimits()
        self.background_enabled = background_enabled
        self._active_by_parent: dict[str, set[str]] = {}
        self._background: dict[str, tuple[str, str, str]] = {}
        self._delivered_by_owner: dict[str, set[str]] = {}

    async def start(
        self,
        spec: SubagentSpec,
        *,
        parent: SubagentParentContext,
        parent_policy: PermissionPolicy,
        available_tools: Mapping[str, Tool],
        tool_snapshot_version: int,
        background: bool = False,
    ) -> tuple[StartedSubagent, TaskHandle]:
        if background and not self.background_enabled:
            raise ToolExecutionError("Background Subagents are disabled")
        child_depth = parent.depth + 1
        if child_depth > self.limits.max_depth:
            raise ToolExecutionError(
                f"Subagent nesting depth exceeds {self.limits.max_depth}"
            )
        active = self._active_tasks(parent.run_id)
        if len(active) >= self.limits.max_active_children:
            raise ToolExecutionError(
                "Subagent active child limit reached: "
                f"{self.limits.max_active_children}"
            )

        run_id = str(uuid4())
        task_id = str(uuid4())
        active.add(task_id)
        child_policy = PermissionPolicy(parent_policy.mode, parent_policy.rules)
        child_parent = SubagentParentContext(
            run_id,
            child_depth,
            task_id,
            parent.owner_run_id,
        )
        try:
            child_catalog = self._child_catalog(
                spec,
                available_tools=available_tools,
                source_version=tool_snapshot_version,
                parent=child_parent,
                policy=child_policy,
            )
            run_spec = AgentRunSpec(
                session=Session(self.project_state_dir, run_id),
                name=spec.description,
                parent_run_id=parent.run_id,
                run_id=run_id,
                tool_catalog=child_catalog,
                permission_policy=child_policy,
                max_steps=self.limits.max_steps,
                max_tokens=self.limits.max_tokens,
                allow_permission_updates=False,
            )
        except BaseException:
            self._release(parent.run_id, task_id)
            raise

        async def execute() -> object:
            run = None
            try:
                run = await self.runs.create(run_spec)
                return await run.submit(AgentTurnInput(spec.prompt, spec.attachments))
            finally:
                try:
                    if run is not None:
                        await run.close()
                finally:
                    self._release(parent.run_id, task_id)

        try:
            handle = await self.tasks.submit(
                execute,
                name=f"subagent:{spec.description}",
                parent_task_id=parent.task_id,
                timeout_seconds=self.limits.timeout_seconds,
                task_id=task_id,
            )
        except BaseException:
            self._release(parent.run_id, task_id)
            raise
        if background:
            self._background[task_id] = (
                parent.owner_run_id,
                run_id,
                spec.description,
            )
        return StartedSubagent(task_id, run_id), handle

    async def run_foreground(
        self,
        spec: SubagentSpec,
        *,
        parent: SubagentParentContext,
        parent_policy: PermissionPolicy,
        available_tools: Mapping[str, Tool],
        tool_snapshot_version: int,
    ) -> CompletedSubagent:
        started, handle = await self.start(
            spec,
            parent=parent,
            parent_policy=parent_policy,
            available_tools=available_tools,
            tool_snapshot_version=tool_snapshot_version,
        )
        try:
            snapshot = await handle.wait()
        except asyncio.CancelledError:
            await handle.cancel("Foreground Subagent wait was cancelled.")
            raise
        outcome = (
            snapshot.result
            if isinstance(
                snapshot.result,
                (AgentTurnSucceeded, AgentMaxStepsReached),
            )
            else None
        )
        if snapshot.status is TaskStatus.SUCCEEDED and outcome is None:
            raise RuntimeError("Subagent task returned an invalid outcome")
        return CompletedSubagent(snapshot, started.run_id, outcome)

    def active_children(self, parent_run_id: str) -> int:
        return len(self._active_tasks(parent_run_id))

    def background_tasks(self, owner_run_id: str) -> tuple[BackgroundSubagent, ...]:
        return tuple(
            BackgroundSubagent(self.tasks.snapshot(task_id), run_id, description)
            for task_id, (owner, run_id, description) in self._background.items()
            if owner == owner_run_id
        )

    def background_task(
        self,
        owner_run_id: str,
        task_id: str,
    ) -> BackgroundSubagent:
        try:
            owner, run_id, description = self._background[task_id]
        except KeyError as error:
            raise ToolExecutionError(f"Unknown background task: {task_id}") from error
        if owner != owner_run_id:
            raise ToolExecutionError(f"Unknown background task: {task_id}")
        return BackgroundSubagent(
            self.tasks.snapshot(task_id),
            run_id,
            description,
        )

    async def cancel_background(
        self,
        owner_run_id: str,
        task_id: str,
    ) -> BackgroundSubagent:
        task = self.background_task(owner_run_id, task_id)
        if not task.task.status.terminal:
            await self.tasks.cancel(
                task_id,
                message="Background Subagent was cancelled by its owner.",
            )
        return self.background_task(owner_run_id, task_id)

    def pending_notifications(
        self,
        owner_run_id: str,
    ) -> tuple[BackgroundSubagent, ...]:
        delivered = self._delivered_by_owner.get(owner_run_id, set())
        return tuple(
            item
            for item in self.background_tasks(owner_run_id)
            if item.task.status.terminal and item.task.task_id not in delivered
        )

    def acknowledge_notifications(
        self,
        owner_run_id: str,
        task_ids: tuple[str, ...],
    ) -> None:
        owned = {item.task.task_id for item in self.background_tasks(owner_run_id)}
        unknown = tuple(task_id for task_id in task_ids if task_id not in owned)
        if unknown:
            raise ValueError(
                "Cannot acknowledge unowned background tasks: " + ", ".join(unknown)
            )
        self._delivered_by_owner.setdefault(owner_run_id, set()).update(task_ids)

    def _child_catalog(
        self,
        spec: SubagentSpec,
        *,
        available_tools: Mapping[str, Tool],
        source_version: int,
        parent: SubagentParentContext,
        policy: PermissionPolicy,
    ) -> ToolCatalog:
        names = (
            tuple(sorted(available_tools))
            if spec.allowed_tools is None
            else spec.allowed_tools
        )
        missing = tuple(name for name in names if name not in available_tools)
        if missing:
            raise ToolExecutionError(
                "Subagent requested unavailable tools: " + ", ".join(missing)
            )
        if parent.depth >= self.limits.max_depth and "Subagent" in names:
            names = tuple(name for name in names if name != "Subagent")

        tools: list[Tool] = []
        for name in names:
            if name == "Subagent":
                from my_code.features.subagents.tool import SubagentTool

                tools.append(SubagentTool(self, parent=parent, policy=policy))
            elif name in {"TaskList", "TaskOutput", "TaskCancel"}:
                from my_code.features.subagents.task_tools import (
                    TaskCancelTool,
                    TaskListTool,
                    TaskOutputTool,
                )

                task_tool_types = {
                    "TaskList": TaskListTool,
                    "TaskOutput": TaskOutputTool,
                    "TaskCancel": TaskCancelTool,
                }
                tools.append(task_tool_types[name](self, parent=parent))
            else:
                tools.append(available_tools[name])
        catalog = ToolCatalog()
        catalog.register_source(
            ToolSourceId("subagent", f"snapshot-{source_version}:{parent.run_id}"),
            tools,
        )
        return catalog

    def _active_tasks(self, parent_run_id: str) -> set[str]:
        active = self._active_by_parent.setdefault(parent_run_id, set())
        terminal = {
            task_id
            for task_id in active
            if self.tasks.snapshot(task_id).status.terminal
        }
        active.difference_update(terminal)
        return active

    def _release(self, parent_run_id: str, task_id: str) -> None:
        active = self._active_by_parent.get(parent_run_id)
        if active is None:
            return
        active.discard(task_id)
        if not active:
            self._active_by_parent.pop(parent_run_id, None)


__all__ = ["SubagentController"]
