"""TASK-01 finite-state and terminal-value coverage."""

import asyncio

import pytest

from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor


@pytest.mark.asyncio
async def test_task_success_has_one_terminal_transition_and_result() -> None:
    supervisor = TaskSupervisor()

    async def succeed() -> object:
        return {"answer": 42}

    handle = await supervisor.submit(succeed, name="success")
    snapshot = await handle.wait()
    statuses = tuple(
        event.snapshot.status
        for event in supervisor.events_after()
        if event.snapshot.task_id == handle.task_id
    )

    assert snapshot.status is TaskStatus.SUCCEEDED
    assert snapshot.result == {"answer": 42}
    assert snapshot.failure is None
    assert statuses == (
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
    )
    assert sum(status.terminal for status in statuses) == 1


@pytest.mark.asyncio
async def test_task_failure_is_structured_and_does_not_escape_wait() -> None:
    supervisor = TaskSupervisor()

    async def fail() -> object:
        raise ValueError("invalid work")

    snapshot = await (await supervisor.submit(fail, name="failure")).wait()

    assert snapshot.status is TaskStatus.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.kind == "ValueError"
    assert snapshot.failure.message == "invalid work"
    assert snapshot.finished_at is not None


@pytest.mark.asyncio
async def test_pending_task_can_be_cancelled_before_runner_starts() -> None:
    supervisor = TaskSupervisor()
    started = False

    async def runner() -> object:
        nonlocal started
        started = True
        return None

    handle = await supervisor.submit(runner, name="pending")
    snapshot = await handle.cancel("cancel before scheduling")

    assert snapshot.status is TaskStatus.CANCELLED
    assert snapshot.started_at is None
    assert snapshot.failure is not None
    assert snapshot.failure.message == "cancel before scheduling"
    assert started is False


@pytest.mark.asyncio
async def test_running_cancellation_cannot_be_suppressed_into_success() -> None:
    supervisor = TaskSupervisor()
    started = asyncio.Event()

    async def suppress_cancellation() -> object:
        started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            return "ignored cancellation"

    handle = await supervisor.submit(suppress_cancellation, name="running")
    await started.wait()
    snapshot = await handle.cancel()

    assert snapshot.status is TaskStatus.CANCELLED
    statuses = tuple(
        event.snapshot.status
        for event in supervisor.events_after()
        if event.snapshot.task_id == handle.task_id
    )
    assert statuses == (
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLING,
        TaskStatus.CANCELLED,
    )
    assert sum(status.terminal for status in statuses) == 1


@pytest.mark.asyncio
async def test_timeout_uses_cancelled_terminal_with_timeout_reason() -> None:
    supervisor = TaskSupervisor()
    started = asyncio.Event()

    async def wait_forever() -> object:
        started.set()
        await asyncio.Future[None]()
        raise AssertionError("unreachable")

    handle = await supervisor.submit(
        wait_forever,
        name="timeout",
        timeout_seconds=0.01,
    )
    await started.wait()
    snapshot = await handle.wait()

    assert snapshot.status is TaskStatus.CANCELLED
    assert snapshot.failure is not None
    assert snapshot.failure.kind == "timeout"
    assert "0.01s" in snapshot.failure.message


@pytest.mark.asyncio
async def test_close_cancels_active_tasks_and_rejects_new_work() -> None:
    supervisor = TaskSupervisor()
    started = asyncio.Event()

    async def wait_forever() -> object:
        started.set()
        await asyncio.Future[None]()
        raise AssertionError("unreachable")

    handle = await supervisor.submit(wait_forever, name="shutdown")
    await started.wait()

    await supervisor.close()

    snapshot = handle.snapshot()
    assert snapshot.status is TaskStatus.CANCELLED
    assert snapshot.failure is not None
    assert snapshot.failure.kind == "shutdown"
    assert supervisor.accepting is False
    with pytest.raises(RuntimeError, match="closed"):
        await supervisor.submit(wait_forever, name="late")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "timeout"])
async def test_terminal_callback_runs_once_for_every_terminal_path(
    outcome: str,
) -> None:
    supervisor = TaskSupervisor()
    snapshots = []
    started = asyncio.Event()

    async def runner() -> object:
        started.set()
        if outcome == "success":
            return "done"
        if outcome == "failure":
            raise ValueError("failed")
        await asyncio.Future[None]()
        raise AssertionError("unreachable")

    handle = await supervisor.submit(
        runner,
        name=outcome,
        timeout_seconds=0.01 if outcome == "timeout" else None,
        on_terminal=snapshots.append,
    )
    await started.wait()
    if outcome == "cancel":
        await handle.cancel()
    else:
        await handle.wait()

    assert snapshots == [handle.snapshot()]
    assert snapshots[0].status.terminal


@pytest.mark.asyncio
async def test_terminal_callback_failure_does_not_change_task_outcome() -> None:
    supervisor = TaskSupervisor()

    def fail_callback(_snapshot: object) -> None:
        raise RuntimeError("callback failed")

    async def succeed() -> object:
        return "result"

    handle = await supervisor.submit(
        succeed,
        name="callback-failure",
        on_terminal=fail_callback,
    )

    snapshot = await handle.wait()
    assert snapshot.status is TaskStatus.SUCCEEDED
    assert snapshot.result == "result"
