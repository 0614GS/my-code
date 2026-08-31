"""Payload-free wakeups shared by all terminal background tasks."""

import asyncio


class BackgroundTaskWakeSignal:
    """A monotonic process-local hint that terminal task state may have changed."""

    def __init__(self) -> None:
        self._revision = 0
        self._changed = asyncio.Event()

    @property
    def revision(self) -> int:
        return self._revision

    def pulse(self) -> None:
        self._revision += 1
        self._changed.set()

    async def wait_for_change(self, after_revision: int) -> int:
        while self._revision <= after_revision:
            self._changed.clear()
            if self._revision > after_revision:
                break
            await self._changed.wait()
        return self._revision


__all__ = ["BackgroundTaskWakeSignal"]
