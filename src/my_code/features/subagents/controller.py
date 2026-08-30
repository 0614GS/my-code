"""Create isolated child Sessions/Runs and supervise their lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import uuid4

from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnSucceeded,
)
from my_code.features.background_tasks.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from my_code.features.subagents.activity import SubagentActivityRecord
from my_code.features.subagents.models import (
    BackgroundSubagent,
    CompletedSubagent,
    StartedSubagent,
    SubagentDefinition,
    SubagentLimits,
    SubagentParentContext,
    SubagentSpec,
    SubagentType,
)
from my_code.features.subagents.read_only import ReadOnlyToolProxy
from my_code.features.subagents.wake import BackgroundTaskWakeSignal
from my_code.permissions.policy import PermissionPolicy
from my_code.runtime.runs import AgentRunFactory, AgentRunSpec
from my_code.sessions.session import Session
from my_code.tasks.models import SubagentTaskView, TaskSnapshot, TaskStatus
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
        tool_results_dir: Callable[[str], Path] | None = None,
        definitions: Mapping[SubagentType, SubagentDefinition],
        limits: SubagentLimits | None = None,
        background_enabled: bool = False,
        wake_signal: BackgroundTaskWakeSignal | None = None,
        background_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        self.runs = runs
        self.tasks = tasks
        self.project_state_dir = project_state_dir
        self.tool_results_dir = tool_results_dir
        self.definitions = definitions
        if set(definitions) != set(SubagentType):
            raise ValueError("Subagent definitions must contain explore and general")
        self.limits = limits or SubagentLimits()
        self.background_enabled = background_enabled
        self.wake_signal = wake_signal
        self.background_registry = background_registry or BackgroundTaskRegistry(
            tasks, wake_signal
        )
        self._active_by_parent: dict[str, set[str]] = {}
        self._activity: dict[str, SubagentActivityRecord] = {}
        self._sessions: dict[str, Session] = {}
        self._activity_revision = 0
        self._activity_changed = asyncio.Event()

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
        definition = self.definitions[spec.agent_type]
        child_parent = SubagentParentContext(
            run_id,
            child_depth,
            task_id,
            parent.owner_run_id,
        )
        activity = SubagentActivityRecord(
            task_id,
            run_id,
            parent.owner_run_id,
            spec.agent_type,
            spec.description,
            background,
            spec.prompt,
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
                session=Session(
                    self.project_state_dir,
                    run_id,
                    tool_results_dir=(
                        self.tool_results_dir(run_id)
                        if self.tool_results_dir is not None
                        else None
                    ),
                ),
                name=spec.description,
                parent_run_id=parent.run_id,
                run_id=run_id,
                tool_catalog=child_catalog,
                permission_policy=child_policy,
                prompt_registry=definition.system_prompt,
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
                outcome = None
                turn_input = AgentTurnInput(spec.prompt, spec.attachments)
                stream = getattr(run, "stream", None)
                if stream is None:
                    # Compatibility for narrow injected test doubles. Real AgentRun
                    # instances always expose the observable stream boundary.
                    outcome = await run.submit(turn_input)
                else:
                    async for event in stream(turn_input):
                        activity.consume(event)
                        self._publish_activity()
                        if isinstance(
                            event, (AgentTurnSucceeded, AgentMaxStepsReached)
                        ):
                            outcome = event
                if outcome is None:
                    raise RuntimeError("Subagent stream ended without a completed turn")
                return outcome
            finally:
                try:
                    if run is not None:
                        await run.close()
                finally:
                    self._release(parent.run_id, task_id)
                    self._publish_activity()

        if background:
            self.background_registry.register(
                BackgroundTask(
                    task_id,
                    parent.owner_run_id,
                    "subagent",
                    spec.description,
                    {
                        "run_id": run_id,
                        "description": spec.description,
                        "agent_type": spec.agent_type.value,
                    },
                )
            )

        def on_terminal(snapshot: TaskSnapshot) -> None:
            if background:
                self.background_registry.terminal(snapshot)
            self._prune_terminal_activity(parent.owner_run_id)
            self._publish_activity()

        try:
            handle = await self.tasks.submit(
                execute,
                name=f"subagent:{spec.description}",
                parent_task_id=parent.task_id,
                timeout_seconds=self.limits.timeout_seconds,
                task_id=task_id,
                on_terminal=on_terminal,
            )
        except BaseException:
            self.background_registry.unregister(task_id)
            self._release(parent.run_id, task_id)
            raise
        self._activity[task_id] = activity
        self._sessions[task_id] = run_spec.session
        self._publish_activity()
        return StartedSubagent(task_id, run_id, spec.agent_type), handle

    @property
    def activity_revision(self) -> int:
        return self._activity_revision

    def task_views(self, owner_run_id: str) -> tuple[SubagentTaskView, ...]:
        records = [
            record
            for record in self._activity.values()
            if record.owner_run_id == owner_run_id
        ]
        active = [
            record
            for record in records
            if not self.tasks.snapshot(record.task_id).status.terminal
        ]
        terminal = [
            record
            for record in records
            if self.tasks.snapshot(record.task_id).status.terminal
        ][-20:]
        return tuple(
            record.view(self.tasks.snapshot(record.task_id))
            for record in (*active, *reversed(terminal))
        )

    def session_for_task(self, task_id: str) -> Session | None:
        return self._sessions.get(task_id)

    async def wait_for_activity(self, after_revision: int) -> int:
        while self._activity_revision <= after_revision:
            self._activity_changed.clear()
            if self._activity_revision > after_revision:
                break
            await self._activity_changed.wait()
        return self._activity_revision

    def _publish_activity(self) -> None:
        self._activity_revision += 1
        self._activity_changed.set()

    def _prune_terminal_activity(self, owner_run_id: str) -> None:
        terminal_ids = [
            task_id
            for task_id, record in self._activity.items()
            if record.owner_run_id == owner_run_id
            and self.tasks.snapshot(task_id).status.terminal
        ]
        for task_id in terminal_ids[:-20]:
            self._activity.pop(task_id, None)
            self._sessions.pop(task_id, None)

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
        return CompletedSubagent(snapshot, started.run_id, outcome, started.agent_type)

    def active_children(self, parent_run_id: str) -> int:
        return len(self._active_tasks(parent_run_id))

    def background_tasks(self, owner_run_id: str) -> tuple[BackgroundSubagent, ...]:
        return tuple(
            BackgroundSubagent(
                self.tasks.snapshot(item.task_id),
                str(item.details["run_id"]),
                str(item.details["description"]),
                SubagentType(str(item.details["agent_type"])),
            )
            for item in self.background_registry.tasks_for(owner_run_id)
            if item.task_type == "subagent"
        )

    def background_task(
        self,
        owner_run_id: str,
        task_id: str,
    ) -> BackgroundSubagent:
        item = self.background_registry.get(owner_run_id, task_id)
        if item.task_type != "subagent":
            raise ToolExecutionError(f"Unknown background task: {task_id}")
        return BackgroundSubagent(
            self.tasks.snapshot(task_id),
            str(item.details["run_id"]),
            str(item.details["description"]),
            SubagentType(str(item.details["agent_type"])),
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
        return tuple(
            self.background_task(owner_run_id, item.task_id)
            for item in self.background_registry.pending(owner_run_id)
            if item.task_type == "subagent"
        )

    def acknowledge_notifications(
        self,
        owner_run_id: str,
        task_ids: tuple[str, ...],
    ) -> None:
        self.background_registry.acknowledge(owner_run_id, task_ids)

    def _child_catalog(
        self,
        spec: SubagentSpec,
        *,
        available_tools: Mapping[str, Tool],
        source_version: int,
        parent: SubagentParentContext,
        policy: PermissionPolicy,
    ) -> ToolCatalog:
        definition = self.definitions[spec.agent_type]
        names = (
            tuple(sorted(available_tools))
            if definition.tool_names is None
            else tuple(
                name for name in definition.tool_names if name in available_tools
            )
        )
        if parent.depth >= self.limits.max_depth and "Subagent" in names:
            names = tuple(name for name in names if name != "Subagent")

        tools: list[Tool] = []
        for name in names:
            if name == "Subagent":
                from my_code.features.subagents.tool import SubagentTool

                tools.append(SubagentTool(self, parent=parent, policy=policy))
            elif name in {"TaskList", "TaskCancel"}:
                from my_code.features.subagents.task_tools import (
                    TaskCancelTool,
                    TaskListTool,
                )

                task_tool_types = {
                    "TaskList": TaskListTool,
                    "TaskCancel": TaskCancelTool,
                }
                tools.append(task_tool_types[name](self, parent=parent))
            else:
                tool = available_tools[name]
                if name == "Bash":
                    from my_code.tools.builtin.bash import BashTool

                    if isinstance(tool, BashTool):
                        tool = tool.foreground_only()
                tools.append(ReadOnlyToolProxy(tool) if definition.read_only else tool)
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
