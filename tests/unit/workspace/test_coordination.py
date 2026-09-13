"""工作区锁的进程内冲突与取消测试。"""

import asyncio
from pathlib import Path

import pytest

from my_code.workspace.local import Workspace


@pytest.mark.asyncio
async def test_workspace_writer_blocks_path_reader(tmp_path: Path) -> None:
    first = Workspace(tmp_path)
    second = Workspace(tmp_path)
    entered = asyncio.Event()

    async def read_path() -> None:
        async with second.coordinator.path_lease(tmp_path / "a.txt", write=False):
            entered.set()

    async with first.coordinator.workspace_lease(write=True):
        waiting = asyncio.create_task(read_path())
        await asyncio.sleep(0.05)
        assert not entered.is_set()

    await asyncio.wait_for(waiting, timeout=1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_different_path_writers_can_overlap(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    second_entered = asyncio.Event()

    async def write_second() -> None:
        async with workspace.coordinator.path_lease(tmp_path / "b.txt", write=True):
            second_entered.set()

    async with workspace.coordinator.path_lease(tmp_path / "a.txt", write=True):
        task = asyncio.create_task(write_second())
        await asyncio.wait_for(second_entered.wait(), timeout=1)
    await task


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_does_not_leak_lease(tmp_path: Path) -> None:
    first = Workspace(tmp_path)
    second = Workspace(tmp_path)

    async with first.coordinator.workspace_lease(write=True):
        waiter = asyncio.create_task(
            second.coordinator.workspace_lease(write=False).__aenter__()
        )
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with second.coordinator.workspace_lease(write=True):
        pass
