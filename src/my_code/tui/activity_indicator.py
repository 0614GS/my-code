"""Pure state and formatting for the live TUI activity indicator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from prompt_toolkit.formatted_text import FormattedText

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class ActivityOwner:
    """Capability token preventing stale tasks from changing a newer activity."""

    revision: int


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    owner: ActivityOwner
    label: str
    started_at: float
    interruptible: bool


class ActivityIndicator:
    """Own the active operation label and its invocation-scoped clock."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._revision = 0
        self._snapshot: ActivitySnapshot | None = None

    @property
    def active(self) -> bool:
        return self._snapshot is not None

    def begin(self, label: str, *, interruptible: bool = False) -> ActivityOwner:
        if not label:
            raise ValueError("Activity label must not be empty")
        self._revision += 1
        owner = ActivityOwner(self._revision)
        self._snapshot = ActivitySnapshot(
            owner,
            label,
            self._clock(),
            interruptible,
        )
        return owner

    def update(self, owner: ActivityOwner, label: str) -> bool:
        """Change a phase label without resetting the operation clock."""

        if not label:
            raise ValueError("Activity label must not be empty")
        snapshot = self._snapshot
        if snapshot is None or snapshot.owner != owner:
            return False
        self._snapshot = ActivitySnapshot(
            owner,
            label,
            snapshot.started_at,
            snapshot.interruptible,
        )
        return True

    def end(self, owner: ActivityOwner) -> bool:
        snapshot = self._snapshot
        if snapshot is None or snapshot.owner != owner:
            return False
        self._snapshot = None
        return True

    def text(self, *, now: float | None = None) -> FormattedText:
        snapshot = self._snapshot
        if snapshot is None:
            return FormattedText()
        current = self._clock() if now is None else now
        elapsed = max(0.0, current - snapshot.started_at)
        frame_index = int(elapsed / _SPINNER_INTERVAL_SECONDS)
        frame = _SPINNER_FRAMES[frame_index % len(_SPINNER_FRAMES)]
        suffix = f"{format_elapsed(int(elapsed))}"
        if snapshot.interruptible:
            suffix += " · Esc to interrupt"
        return FormattedText(
            [
                ("class:prompt", f"{frame} "),
                ("", snapshot.label),
                ("class:secondary", f" ({suffix})"),
            ]
        )


def format_elapsed(seconds: int) -> str:
    """Format elapsed whole seconds without changing width unnecessarily."""

    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


__all__ = [
    "ActivityIndicator",
    "ActivityOwner",
    "ActivitySnapshot",
    "format_elapsed",
]
