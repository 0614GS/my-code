"""工作区内跨任务、跨进程的协作锁。"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class WorkspaceCoordinator:
    """用工作区总锁和路径锁约束所有协作型工具。"""

    def __init__(self, workspace: Path, lock_root: Path | None = None) -> None:
        resolved = workspace.resolve()
        workspace_key = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:32]
        uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
        base = lock_root or Path(tempfile.gettempdir()) / f"my-code-{uid}" / "locks"
        self.root = base / workspace_key
        self._prepare_directory(base)
        self._prepare_directory(self.root)
        self._workspace_lock = self.root / "workspace.lock"

    @asynccontextmanager
    async def path_lease(self, path: Path, *, write: bool) -> AsyncIterator[None]:
        """路径操作先持有工作区共享锁，再持有对应路径锁。"""

        digest = _path_digest(path)
        async with self._lease(self._workspace_lock, write=False):
            async with self._lease(self.root / f"path-{digest}.lock", write=write):
                yield

    @asynccontextmanager
    async def workspace_lease(self, *, write: bool) -> AsyncIterator[None]:
        """未知写集合使用工作区独占锁覆盖完整执行生命周期。"""

        async with self._lease(self._workspace_lock, write=write):
            yield

    @asynccontextmanager
    async def _lease(self, path: Path, *, write: bool) -> AsyncIterator[None]:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
        try:
            while True:
                try:
                    fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)


__all__ = ["WorkspaceCoordinator"]


def _path_digest(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path.resolve(strict=False))).hexdigest()
