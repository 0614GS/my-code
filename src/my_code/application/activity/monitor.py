"""Subagent activity revision monitoring."""

import asyncio
from collections.abc import AsyncIterator, Callable

from my_code.application.contracts.views import SubagentTaskView
from my_code.features.subagents.controller import SubagentController


class ActivityMonitor:
    def __init__(self, subagents: SubagentController | None) -> None:
        self._subagents = subagents

    async def stream(
        self,
        project: Callable[[], tuple[SubagentTaskView, ...]],
    ) -> AsyncIterator[tuple[SubagentTaskView, ...]]:
        if self._subagents is None:
            return
        revision = -1
        while True:
            current = self._subagents.activity_revision
            if current != revision:
                revision = current
                yield project()
            try:
                revision = await asyncio.wait_for(
                    self._subagents.wait_for_activity(revision), timeout=1.0
                )
            except TimeoutError:
                current_view = project()
                if any(
                    item.status in {"pending", "running", "cancelling"}
                    for item in current_view
                ):
                    yield current_view


__all__ = ["ActivityMonitor"]
