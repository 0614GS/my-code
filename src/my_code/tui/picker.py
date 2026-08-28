"""Shared selection and viewport primitives for composer-attached pickers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PickerRow:
    """One displayed row and the stable action key selected by Enter."""

    key: str
    label: str
    trailing: str | None = None


@dataclass(frozen=True, slots=True)
class PickerView:
    """Immutable presentation of one picker page."""

    title: str
    rows: tuple[PickerRow, ...]
    hint: str
    visible_count: int = 7


@dataclass(slots=True)
class PickerState:
    """Keep selection visible while preserving stable row identity."""

    index: int = 0
    offset: int = 0
    selected_key: str | None = None

    def reset(self, index: int = 0) -> None:
        self.index = max(0, index)
        self.offset = 0
        self.selected_key = None

    def sync(self, rows: tuple[PickerRow, ...]) -> None:
        if not rows:
            self.index = 0
            self.offset = 0
            self.selected_key = None
            return
        if self.selected_key is not None:
            matching = next(
                (i for i, row in enumerate(rows) if row.key == self.selected_key),
                None,
            )
            if matching is not None:
                self.index = matching
        self.index = min(max(self.index, 0), len(rows) - 1)
        self.selected_key = rows[self.index].key

    def move(self, rows: tuple[PickerRow, ...], offset: int) -> None:
        self.sync(rows)
        if not rows:
            return
        self.index = min(max(self.index + offset, 0), len(rows) - 1)
        self.selected_key = rows[self.index].key

    def visible(
        self, rows: tuple[PickerRow, ...], limit: int
    ) -> tuple[int, tuple[PickerRow, ...]]:
        self.sync(rows)
        if not rows:
            return 0, ()
        size = max(1, limit)
        max_offset = max(0, len(rows) - size)
        self.offset = min(self.offset, max_offset)
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + size:
            self.offset = self.index - size + 1
        return self.offset, rows[self.offset : self.offset + size]

    def current(self, rows: tuple[PickerRow, ...]) -> PickerRow | None:
        self.sync(rows)
        return rows[self.index] if rows else None


__all__ = ["PickerRow", "PickerState", "PickerView"]
