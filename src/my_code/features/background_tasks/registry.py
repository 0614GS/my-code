"""Owner-scoped metadata and single-delivery coordination for background tasks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from my_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from my_code.foundation.json import JsonObject
from my_code.tasks.models import TaskSnapshot
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import ToolExecutionError


@dataclass(slots=True)
class BackgroundTask:
    task_id: str
    owner_run_id: str
    task_type: str
    summary: str
    details: JsonObject = field(default_factory=dict)


class BackgroundWakeSignal(Protocol):
    @property
    def revision(self) -> int: ...

    def pulse(self) -> None: ...


class BackgroundTaskRegistry:
    """Product-level ownership/delivery registry over the generic supervisor."""

    def __init__(
        self,
        tasks: TaskSupervisor,
        wake_signal: BackgroundWakeSignal | None = None,
    ) -> None:
        self.tasks = tasks
        self.wake_signal = wake_signal
        self._records: dict[str, BackgroundTask] = {}
        self._delivered: dict[str, set[str]] = {}
        self._pulsed: set[str] = set()

    def register(self, item: BackgroundTask) -> None:
        if item.task_id in self._records:
            raise ValueError(f"Background task already registered: {item.task_id}")
        self._records[item.task_id] = item

    def unregister(self, task_id: str) -> None:
        self._records.pop(task_id, None)

    def terminal(self, snapshot: TaskSnapshot) -> None:
        if snapshot.task_id not in self._records or snapshot.task_id in self._pulsed:
            return
        self._pulsed.add(snapshot.task_id)
        signal = self.wake_signal
        if signal is not None:
            signal.pulse()

    def tasks_for(self, owner_run_id: str) -> tuple[BackgroundTask, ...]:
        return tuple(
            item for item in self._records.values() if item.owner_run_id == owner_run_id
        )

    def get(self, owner_run_id: str, task_id: str) -> BackgroundTask:
        item = self._records.get(task_id)
        if item is None or item.owner_run_id != owner_run_id:
            raise ToolExecutionError(f"Unknown background task: {task_id}")
        return item

    async def cancel(self, owner_run_id: str, task_id: str) -> BackgroundTask:
        item = self.get(owner_run_id, task_id)
        snapshot = self.tasks.snapshot(task_id)
        if not snapshot.status.terminal:
            await self.tasks.cancel(
                task_id, message="Background task was cancelled by its owner."
            )
        return item

    def pending(self, owner_run_id: str) -> tuple[BackgroundTask, ...]:
        delivered = self._delivered.get(owner_run_id, set())
        return tuple(
            item
            for item in self.tasks_for(owner_run_id)
            if self.tasks.snapshot(item.task_id).status.terminal
            and item.task_id not in delivered
        )

    def acknowledge(self, owner_run_id: str, task_ids: tuple[str, ...]) -> None:
        owned = {item.task_id for item in self.tasks_for(owner_run_id)}
        unknown = tuple(task_id for task_id in task_ids if task_id not in owned)
        if unknown:
            raise ValueError(
                "Cannot acknowledge unowned background tasks: " + ", ".join(unknown)
            )
        self._delivered.setdefault(owner_run_id, set()).update(task_ids)

    def payload(self, item: BackgroundTask) -> JsonObject:
        task = self.tasks.snapshot(item.task_id)
        payload: JsonObject = {
            "task_id": task.task_id,
            "task_type": item.task_type,
            "summary": item.summary,
            "status": task.status.value,
            "created_at": task.created_at,
        }
        if task.started_at is not None:
            payload["started_at"] = task.started_at
        if task.finished_at is not None:
            payload["finished_at"] = task.finished_at
        payload.update(item.details)
        if task.failure is not None:
            payload.setdefault("error_kind", task.failure.kind)
            payload.setdefault("error", task.failure.message)
        if isinstance(task.result, AgentTurnSucceeded):
            payload["result"] = task.result.text
            payload["completed_steps"] = task.result.completed_steps
        elif isinstance(task.result, AgentMaxStepsReached):
            payload["result_status"] = "max_steps"
            payload["completed_steps"] = task.result.completed_steps
            payload["max_steps"] = task.result.max_steps
        return payload


def secure_task_output_path(directory: Path, task_id: str) -> Path:
    """Create private parents and an empty non-symlink task output file."""

    from uuid import UUID

    if str(UUID(task_id)) != task_id:
        raise ValueError("Task ID must be a canonical UUID")
    existing_parent = directory
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = directory
    while current != existing_parent:
        current.chmod(0o700)
        current = current.parent
    if existing_parent.name.startswith("my-code-"):
        existing_parent.chmod(0o700)
    path = directory / f"{task_id}.output"
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Task output already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(path, flags, 0o600))
    return path


__all__ = ["BackgroundTask", "BackgroundTaskRegistry", "BackgroundWakeSignal"]
