"""Semantic grouping for completed model display blocks."""

from dataclasses import dataclass
from typing import Literal

from rich.console import RenderableType

from my_code.model.primitives import ReasoningPresentation
from my_code.tui.widgets import (
    assistant_message,
    block_separator,
    reasoning_message,
    work_separator,
)


@dataclass(frozen=True, slots=True)
class _PendingBlock:
    kind: Literal["reasoning", "text"]
    value: ReasoningPresentation | str


class TurnBlockCoordinator:
    """Classify one or more model steps into a Codex-style work group."""

    def __init__(self) -> None:
        self._pending: list[_PendingBlock] = []
        self._work_visible = False

    def add_text(self, text: str) -> None:
        self._pending.append(_PendingBlock("text", text))

    def add_reasoning(self, presentation: ReasoningPresentation) -> None:
        self._pending.append(_PendingBlock("reasoning", presentation))

    def mark_work(self) -> None:
        self._work_visible = True

    def reset_group(self) -> None:
        self._pending.clear()
        self._work_visible = False

    def complete_step(
        self, *, has_tools: bool, label_answer: bool = False
    ) -> tuple[RenderableType, ...]:
        pending = tuple(self._pending)
        self._pending.clear()
        if has_tools:
            if pending:
                self._work_visible = True
            return tuple(_render(block) for block in pending)

        renderables: list[RenderableType] = []
        separator_written = False
        reasoning_in_step = False
        has_answer = any(block.kind == "text" for block in pending)
        for block in pending:
            if block.kind == "reasoning":
                reasoning_in_step = True
                renderables.append(_render(block))
                continue
            if not separator_written and (
                label_answer or self._work_visible or reasoning_in_step
            ):
                renderables.append(
                    block_separator("Assistant response")
                    if label_answer
                    else work_separator()
                )
                separator_written = True
            renderables.append(_render(block))

        if has_answer:
            self._work_visible = False
        elif reasoning_in_step:
            self._work_visible = True
        return tuple(renderables)

    def drain_unclassified(self) -> tuple[RenderableType, ...]:
        pending = tuple(self._pending)
        self._pending.clear()
        if pending:
            self._work_visible = True
        return tuple(_render(block) for block in pending)


def _render(block: _PendingBlock) -> RenderableType:
    if block.kind == "text":
        assert isinstance(block.value, str)
        return assistant_message(block.value)
    assert isinstance(block.value, ReasoningPresentation)
    return reasoning_message(block.value)


__all__ = ["TurnBlockCoordinator"]
