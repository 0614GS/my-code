"""Lifecycle coordination for the TUI activity indicator."""

from __future__ import annotations

import asyncio

from my_code.tui.activity_indicator import (
    ActivityIndicator,
    ActivityOwner,
)


class ActivityFlowMixin:
    """Own one animated activity and reject updates from stale operations."""

    _running: bool
    _activity_indicator: ActivityIndicator
    _activity_changed: asyncio.Event
    _agent_activity_owner: ActivityOwner | None

    def _initialize_activity_flow(self) -> None:
        self._activity_indicator = ActivityIndicator()
        self._activity_changed = asyncio.Event()
        self._agent_activity_owner = None

    def _invalidate(self) -> None:
        raise NotImplementedError

    def _begin_activity(
        self, label: str, *, interruptible: bool = False
    ) -> ActivityOwner:
        owner = self._activity_indicator.begin(label, interruptible=interruptible)
        self._activity_changed.set()
        self._invalidate()
        return owner

    def _update_activity(self, owner: ActivityOwner | None, label: str) -> None:
        if owner is not None and self._activity_indicator.update(owner, label):
            self._activity_changed.set()
            self._invalidate()

    def _end_activity(self, owner: ActivityOwner | None) -> None:
        if owner is not None and self._activity_indicator.end(owner):
            self._activity_changed.set()
            self._invalidate()

    def _begin_agent_activity(self, label: str) -> None:
        self._agent_activity_owner = self._begin_activity(label, interruptible=True)

    def _update_agent_activity(self, label: str) -> None:
        self._update_activity(self._agent_activity_owner, label)

    def _end_agent_activity(self) -> None:
        self._end_activity(self._agent_activity_owner)
        self._agent_activity_owner = None

    async def _tick_activity(self) -> None:
        """Drive only the cheap status animation while an activity is visible."""

        while self._running:
            if not self._activity_indicator.active:
                self._activity_changed.clear()
                await self._activity_changed.wait()
                continue
            try:
                self._activity_changed.clear()
                await asyncio.wait_for(self._activity_changed.wait(), timeout=0.1)
            except TimeoutError:
                self._invalidate()


__all__ = ["ActivityFlowMixin"]
