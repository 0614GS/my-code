"""TASK-02 parent cancellation closes a complete supervised task tree."""

import asyncio

import pytest

from my_code.tasks.models import TaskStatus
from my_code.tasks.supervisor import TaskSupervisor


@pytest.mark.asyncio
async def test_parent_cancellation_reaps_every_descendant_task() -> None:
    supervisor = TaskSupervisor()
    started = {name: asyncio.Event() for name in ("parent", "child", "grandchild")}

    def blocked(name: str):  # type: ignore[no-untyped-def]
        async def run() -> object:
            started[name].set()
            await asyncio.Future[None]()
            raise AssertionError("unreachable")

        return run

    parent = await supervisor.submit(blocked("parent"), name="parent")
    child = await supervisor.submit(
        blocked("child"),
        name="child",
        parent_task_id=parent.task_id,
    )
    grandchild = await supervisor.submit(
        blocked("grandchild"),
        name="grandchild",
        parent_task_id=child.task_id,
    )
    await asyncio.gather(*(event.wait() for event in started.values()))

    parent_snapshot = await parent.cancel("parent cancelled")

    assert parent_snapshot.status is TaskStatus.CANCELLED
    assert child.snapshot().status is TaskStatus.CANCELLED
    assert grandchild.snapshot().status is TaskStatus.CANCELLED
    for snapshot in supervisor.snapshots():
        assert snapshot.failure is not None
        assert snapshot.failure.message == "parent cancelled"
    assert not any(
        not task.done() and task.get_name().startswith("my-code:")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )
