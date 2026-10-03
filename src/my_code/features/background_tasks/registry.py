"""Owner-scoped metadata and single-delivery coordination for background tasks."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from my_code.agent.models import AgentInvocationSucceeded, AgentMaxStepsReached
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
        self._watched: dict[str, set[str]] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}

    def register(self, item: BackgroundTask) -> None:
        if item.task_id in self._records:
            raise ValueError(f"Background task already registered: {item.task_id}")
        self._records[item.task_id] = item

    def unregister(self, task_id: str) -> None:
        self._records.pop(task_id, None)
        for watched in self._watched.values():
            watched.discard(task_id)

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
        self.watch(owner_run_id, task_id, enabled=False)
        snapshot = self.tasks.snapshot(task_id)
        if not snapshot.status.terminal:
            await self.tasks.cancel(
                task_id, message="Background task was cancelled by its owner."
            )
        return item

    def watch(self, owner_run_id: str, task_id: str, *, enabled: bool) -> bool:
        """只订阅仍在运行且尚未投递的任务。"""

        self.get(owner_run_id, task_id)
        watched = self._watched.setdefault(owner_run_id, set())
        if not enabled:
            watched.discard(task_id)
            return False
        if self.tasks.snapshot(
            task_id
        ).status.terminal or task_id in self._delivered.get(owner_run_id, set()):
            watched.discard(task_id)
            return False
        watched.add(task_id)
        return True

    def watched_pending(self, owner_run_id: str) -> tuple[BackgroundTask, ...]:
        watched = self._watched.get(owner_run_id, set())
        return tuple(
            item for item in self.pending(owner_run_id) if item.task_id in watched
        )

    def interrupt_waiters(self, owner_run_id: str) -> None:
        for event in tuple(self._waiters.get(owner_run_id, ())):
            event.set()

    def interrupt_all_waiters(self) -> None:
        for owner in tuple(self._waiters):
            self.interrupt_waiters(owner)

    async def wait(
        self, owner_run_id: str, task_id: str, timeout_seconds: float
    ) -> str:
        """等待终态或用户输入；取消等待不影响被监督任务。"""

        self.get(owner_run_id, task_id)
        if self.tasks.snapshot(task_id).status.terminal:
            return "completed"
        interrupted = asyncio.Event()
        self._waiters.setdefault(owner_run_id, set()).add(interrupted)
        task_wait = asyncio.create_task(self.tasks.wait(task_id))
        input_wait = asyncio.create_task(interrupted.wait())
        try:
            done, _ = await asyncio.wait(
                (task_wait, input_wait),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupted.is_set():
                return "user_input"
            return "completed" if task_wait in done else "timeout"
        finally:
            task_wait.cancel()
            input_wait.cancel()
            waiters = self._waiters[owner_run_id]
            waiters.discard(interrupted)
            if not waiters:
                self._waiters.pop(owner_run_id, None)

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
        self._watched.setdefault(owner_run_id, set()).difference_update(task_ids)

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
        if isinstance(task.result, AgentInvocationSucceeded):
            payload["result"] = task.result.text
            payload["completed_steps"] = task.result.completed_steps
        elif isinstance(task.result, AgentMaxStepsReached):
            payload["result_status"] = "max_steps"
            payload["completed_steps"] = task.result.completed_steps
            payload["max_steps"] = task.result.max_steps
        if item.task_type == "bash":
            payload.update(self.output_preview(item, terminal=task.status.terminal))
        return payload

    def output_preview(self, item: BackgroundTask, *, terminal: bool) -> JsonObject:
        path = item.details.get("output_file")
        if item.task_type != "bash" or not isinstance(path, str):
            return {}
        limit = 4096 if terminal else 1024
        line_limit = 40 if terminal else 5
        raw, total = _read_output(Path(path), -limit, limit)
        lines = raw.decode("utf-8", errors="replace").splitlines()[-line_limit:]
        preview = "\n".join(lines)
        encoded_preview = preview.encode("utf-8")
        if len(encoded_preview) > limit:
            preview = encoded_preview[-limit:].decode("utf-8", errors="ignore")
        return {
            "output_preview": preview,
            "output_total_bytes": total,
            "output_truncated": total > len(preview.encode("utf-8")),
        }

    def output_since(self, item: BackgroundTask, offset: int) -> JsonObject:
        path = item.details.get("output_file")
        if item.task_type != "bash" or not isinstance(path, str):
            return {"output": "", "next_output_offset": offset, "omitted_bytes": 0}
        raw, total, omitted = _read_output_since(Path(path), offset)
        output = raw.decode("utf-8", errors="replace")
        encoded_output = output.encode("utf-8")
        display_truncated = len(encoded_output) > 8192
        if display_truncated:
            output = encoded_output[:4096].decode(
                "utf-8", errors="ignore"
            ) + encoded_output[-4096:].decode("utf-8", errors="ignore")
        return {
            "output": output,
            "next_output_offset": total,
            "output_total_bytes": total,
            "omitted_bytes": omitted,
            "output_display_truncated": display_truncated,
        }


def _read_output(path: Path, offset: int, limit: int) -> tuple[bytes, int]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return b"", 0
    with os.fdopen(fd, "rb") as stream:
        total = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, total + offset) if offset < 0 else offset)
        return stream.read(limit), total


def _read_output_since(path: Path, offset: int) -> tuple[bytes, int, int]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return b"", 0, 0
    with os.fdopen(fd, "rb") as stream:
        total = os.fstat(stream.fileno()).st_size
        start = min(max(offset, 0), total)
        available = total - start
        stream.seek(start)
        if available <= 8192:
            return stream.read(available), total, 0
        head = stream.read(4096)
        stream.seek(total - 4096)
        tail = stream.read(4096)
        return head + tail, total, available - 8192


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
