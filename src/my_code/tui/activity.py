"""Ordered presentation state for one contiguous run of tool activity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from my_code.tools.presentation import ToolUsePresentation


class ToolActivityResult(Protocol):
    @property
    def summary(self) -> str: ...

    @property
    def detail(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _InterruptedResult:
    summary: str = "Interrupted"
    detail: str | None = None


@dataclass(slots=True)
class ToolActivityItem:
    tool_use_id: str
    use: ToolUsePresentation
    result: ToolActivityResult | None = None
    is_error: bool = False

    @property
    def running(self) -> bool:
        return self.result is None


class ToolActivityGroup:
    """Keep launch order stable while results arrive in any order."""

    def __init__(self) -> None:
        self._items: list[ToolActivityItem] = []
        self._by_id: dict[str, ToolActivityItem] = {}

    @property
    def items(self) -> tuple[ToolActivityItem, ...]:
        return tuple(self._items)

    def start(self, tool_use_id: str, use: ToolUsePresentation) -> None:
        if tool_use_id in self._by_id:
            return
        item = ToolActivityItem(tool_use_id, use)
        self._items.append(item)
        self._by_id[tool_use_id] = item

    def finish(
        self,
        tool_use_id: str,
        result: ToolActivityResult,
        *,
        is_error: bool,
    ) -> ToolActivityItem | None:
        item = self._by_id.get(tool_use_id)
        if item is None:
            return None
        item.result = result
        item.is_error = is_error
        return item

    def remove(self, tool_use_id: str) -> None:
        item = self._by_id.pop(tool_use_id, None)
        if item is not None:
            self._items.remove(item)

    def interrupt_running(self) -> None:
        for item in self._items:
            if item.running:
                item.result = _InterruptedResult()
                item.is_error = True

    def __bool__(self) -> bool:
        return bool(self._items)


__all__ = ["ToolActivityGroup", "ToolActivityItem"]
