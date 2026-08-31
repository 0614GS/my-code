"""Session-bound, in-memory steering input preparation and FIFO ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from my_code.agent.models import UserTurnInput
from my_code.application.contracts.inputs import QueuedInputView, QueueInputState
from my_code.application.turns.mentions.loader import AttachmentLoader


@dataclass(slots=True)
class _PendingInput:
    input_id: str
    prompt: str
    task: asyncio.Task[tuple[Any, ...]]
    state: QueueInputState = QueueInputState.PREPARING
    error: str | None = None


class PendingInputController:
    """Prepare attachments eagerly while retaining persistence ownership."""

    def __init__(
        self, session_id: str, attachment_loader: AttachmentLoader | None
    ) -> None:
        self._session_id = session_id
        self._loader = attachment_loader
        self._items: list[_PendingInput] = []
        self._reported_failures: set[str] = set()

    @property
    def session_id(self) -> str:
        return self._session_id

    def queue_input(self, prompt: str) -> QueuedInputView:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        task = asyncio.create_task(self._prepare(prompt))
        item = _PendingInput(str(uuid4()), prompt, task)
        self._items.append(item)
        task.add_done_callback(lambda completed: self._prepared(item, completed))
        return self._view(item)

    async def _prepare(self, prompt: str) -> tuple[Any, ...]:
        if self._loader is None:
            return ()
        return tuple(await self._loader.load(prompt))

    def _prepared(
        self, item: _PendingInput, task: asyncio.Task[tuple[Any, ...]]
    ) -> None:
        if item not in self._items or task.cancelled():
            return
        error = task.exception()
        if error is None:
            item.state = QueueInputState.QUEUED
        else:
            item.state = QueueInputState.FAILED
            item.error = str(error)

    async def prepare_pending(self) -> None:
        tasks = tuple(
            item.task for item in self._items if item.state is QueueInputState.PREPARING
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain_pending(self) -> tuple[UserTurnInput, ...]:
        # Snapshot the boundary. Inputs submitted during this await wait for the
        # following boundary, preserving a deterministic FIFO cut.
        snapshot = tuple(self._items)
        tasks = tuple(
            item.task for item in snapshot if item.state is QueueInputState.PREPARING
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        ready: list[UserTurnInput] = []
        for item in snapshot:
            if item.state is not QueueInputState.QUEUED:
                continue
            loaded = item.task.result()
            attachments = tuple(entry.attachment for entry in loaded)
            ready.append(UserTurnInput(item.prompt, attachments, item.input_id))
        return tuple(ready)

    def accept_pending(self, input_ids: Sequence[str]) -> None:
        accepted = set(input_ids)
        self._items = [item for item in self._items if item.input_id not in accepted]

    def recall_latest_input(self) -> str | None:
        if not self._items:
            return None
        item = self._items.pop()
        self._reported_failures.discard(item.input_id)
        if not item.task.done():
            item.task.cancel()
        return item.prompt

    def queued_inputs(self) -> tuple[QueuedInputView, ...]:
        return tuple(self._view(item) for item in self._items)

    def drain_failures(self) -> tuple[QueuedInputView, ...]:
        failures = tuple(
            self._view(item)
            for item in self._items
            if item.state is QueueInputState.FAILED
            and item.input_id not in self._reported_failures
        )
        self._reported_failures.update(item.input_id for item in failures)
        return failures

    def has_actionable(self) -> bool:
        return any(item.state is not QueueInputState.FAILED for item in self._items)

    def clear(self) -> None:
        for item in self._items:
            if not item.task.done():
                item.task.cancel()
        self._items.clear()
        self._reported_failures.clear()

    @staticmethod
    def _view(item: _PendingInput) -> QueuedInputView:
        return QueuedInputView(item.input_id, item.prompt, item.state, item.error)


__all__ = ["PendingInputController"]
