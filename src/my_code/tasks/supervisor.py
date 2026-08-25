"""In-process structured task supervision with cancellation trees."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from my_code.tasks.models import (
    TaskEvent,
    TaskFailure,
    TaskSnapshot,
    TaskStatus,
)

type TaskRunner = Callable[[], Awaitable[object]]


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    name: str
    parent_task_id: str | None
    status: TaskStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: object | None = None
    failure: TaskFailure | None = None
    task: asyncio.Task[None] | None = None
    cancel_reason: TaskFailure | None = None


class TaskHandle:
    """Stable capability for observing or cancelling one supervised task."""

    def __init__(self, supervisor: TaskSupervisor, task_id: str) -> None:
        self._supervisor = supervisor
        self.task_id = task_id

    def snapshot(self) -> TaskSnapshot:
        return self._supervisor.snapshot(self.task_id)

    async def wait(self) -> TaskSnapshot:
        return await self._supervisor.wait(self.task_id)

    async def cancel(self, message: str = "Task was cancelled.") -> TaskSnapshot:
        return await self._supervisor.cancel(self.task_id, message=message)


class TaskSupervisor:
    """Own every process-local background task and its finite state machine."""

    def __init__(self) -> None:
        self._records: dict[str, _TaskRecord] = {}
        self._events: list[TaskEvent] = []
        self._accepting = True

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def submit(
        self,
        runner: TaskRunner,
        *,
        name: str,
        parent_task_id: str | None = None,
        timeout_seconds: float | None = None,
        task_id: str | None = None,
    ) -> TaskHandle:
        if not self._accepting:
            raise RuntimeError("Task supervisor is closed")
        if not name.strip():
            raise ValueError("Task name must not be blank")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Task timeout must be positive or null")
        if parent_task_id is not None:
            parent = self._record(parent_task_id)
            if parent.status.terminal:
                raise ValueError("Cannot attach a task to a terminal parent")

        actual_task_id = task_id or str(uuid4())
        if not actual_task_id.strip():
            raise ValueError("Task ID must not be blank")
        if actual_task_id in self._records:
            raise ValueError(f"Task ID already exists: {actual_task_id}")
        record = _TaskRecord(
            task_id=actual_task_id,
            name=name,
            parent_task_id=parent_task_id,
            status=TaskStatus.PENDING,
            created_at=_now(),
        )
        self._records[actual_task_id] = record
        self._emit(record)
        record.task = asyncio.create_task(
            self._run(record, runner, timeout_seconds),
            name=f"my-code:{name}:{actual_task_id}",
        )
        return TaskHandle(self, actual_task_id)

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return _snapshot(self._record(task_id))

    def snapshots(self) -> tuple[TaskSnapshot, ...]:
        return tuple(_snapshot(record) for record in self._records.values())

    def events_after(self, sequence: int = -1) -> tuple[TaskEvent, ...]:
        return tuple(event for event in self._events if event.sequence > sequence)

    async def wait(self, task_id: str) -> TaskSnapshot:
        record = self._record(task_id)
        task = record.task
        if task is not None and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not record.status.terminal:
                    raise
        return _snapshot(record)

    async def cancel(
        self,
        task_id: str,
        *,
        message: str = "Task was cancelled.",
    ) -> TaskSnapshot:
        if not message.strip():
            raise ValueError("Task cancellation message must not be blank")
        root = self._record(task_id)
        await self._cancel_records(
            self._tree_records(root.task_id),
            TaskFailure("cancelled", message),
        )
        return _snapshot(root)

    async def close(self) -> None:
        if not self._accepting and all(
            record.status.terminal for record in self._records.values()
        ):
            return
        self._accepting = False
        active = tuple(
            record for record in self._records.values() if not record.status.terminal
        )
        await self._cancel_records(
            active,
            TaskFailure("shutdown", "Application runtime is shutting down."),
        )

    async def _run(
        self,
        record: _TaskRecord,
        runner: TaskRunner,
        timeout_seconds: float | None,
    ) -> None:
        if record.status is TaskStatus.CANCELLED:
            return
        record.status = TaskStatus.RUNNING
        record.started_at = _now()
        self._emit(record)
        try:
            if timeout_seconds is None:
                result = await runner()
            else:
                async with asyncio.timeout(timeout_seconds):
                    result = await runner()
        except TimeoutError:
            failure = TaskFailure(
                "timeout",
                f"Task exceeded its {timeout_seconds:g}s timeout.",
            )
            await self._cancel_descendants(record.task_id, failure)
            self._finish_cancelled(record, failure)
        except asyncio.CancelledError:
            failure = record.cancel_reason or TaskFailure(
                "cancelled", "Task was cancelled."
            )
            await self._cancel_descendants(record.task_id, failure)
            self._finish_cancelled(record, failure)
        except Exception as error:
            record.status = TaskStatus.FAILED
            record.failure = TaskFailure(
                type(error).__name__, str(error) or type(error).__name__
            )
            record.finished_at = _now()
            self._emit(record)
        else:
            if record.status is TaskStatus.CANCELLING:
                self._finish_cancelled(
                    record,
                    record.cancel_reason
                    or TaskFailure("cancelled", "Task was cancelled."),
                )
            else:
                record.status = TaskStatus.SUCCEEDED
                record.result = result
                record.finished_at = _now()
                self._emit(record)

    async def _cancel_descendants(
        self,
        task_id: str,
        failure: TaskFailure,
    ) -> None:
        descendants = tuple(
            record
            for record in self._tree_records(task_id)
            if record.task_id != task_id
        )
        await self._cancel_records(descendants, failure)

    async def _cancel_records(
        self,
        records: tuple[_TaskRecord, ...],
        failure: TaskFailure,
    ) -> None:
        tasks: list[asyncio.Task[None]] = []
        for record in reversed(records):
            if record.status.terminal:
                continue
            record.cancel_reason = failure
            task = record.task
            if record.status is TaskStatus.PENDING:
                record.status = TaskStatus.CANCELLED
                record.failure = failure
                record.finished_at = _now()
                self._emit(record)
            elif record.status is TaskStatus.RUNNING:
                record.status = TaskStatus.CANCELLING
                self._emit(record)
            if task is not None and not task.done():
                task.cancel()
                if task is not asyncio.current_task():
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _tree_records(self, task_id: str) -> tuple[_TaskRecord, ...]:
        ordered: list[_TaskRecord] = []

        def visit(parent_id: str) -> None:
            parent = self._record(parent_id)
            ordered.append(parent)
            for candidate in tuple(self._records.values()):
                if candidate.parent_task_id == parent_id:
                    visit(candidate.task_id)

        visit(task_id)
        return tuple(ordered)

    def _finish_cancelled(
        self,
        record: _TaskRecord,
        failure: TaskFailure,
    ) -> None:
        if record.status is TaskStatus.CANCELLED:
            return
        record.status = TaskStatus.CANCELLED
        record.failure = failure
        record.finished_at = _now()
        self._emit(record)

    def _record(self, task_id: str) -> _TaskRecord:
        try:
            return self._records[task_id]
        except KeyError as error:
            raise KeyError(f"Unknown task: {task_id}") from error

    def _emit(self, record: _TaskRecord) -> None:
        self._events.append(TaskEvent(len(self._events), _snapshot(record)))


def _snapshot(record: _TaskRecord) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=record.task_id,
        name=record.name,
        parent_task_id=record.parent_task_id,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        result=record.result,
        failure=record.failure,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "TaskHandle",
    "TaskRunner",
    "TaskSupervisor",
]
