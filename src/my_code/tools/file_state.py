"""模型已观察文件版本的会话级记录。"""

from dataclasses import dataclass, field
from pathlib import Path

from my_code.workspace.local import FileFingerprint


@dataclass(slots=True)
class _ObservedFile:
    fingerprint: FileFingerprint
    total_lines: int
    intervals: list[tuple[int, int]] = field(default_factory=list)


class FileReadTracker:
    """只授权模型实际看到且仍保持相同版本的完整文件。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[Path, _ObservedFile]] = {}

    def record_range(
        self,
        session_key: str,
        path: Path,
        fingerprint: FileFingerprint,
        *,
        total_lines: int,
        start: int | None,
        end: int | None,
        complete_lines: bool,
    ) -> None:
        files = self._sessions.setdefault(session_key, {})
        observed = files.get(path)
        if observed is None or observed.fingerprint != fingerprint:
            observed = _ObservedFile(fingerprint, total_lines)
            files[path] = observed
        if total_lines == 0:
            return
        if not complete_lines or start is None or end is None:
            return
        observed.intervals = _merge((*observed.intervals, (start, end)))

    def require_complete(self, session_key: str, path: Path) -> FileFingerprint | None:
        observed = self._sessions.get(session_key, {}).get(path)
        if observed is None:
            return None
        if observed.total_lines == 0:
            return observed.fingerprint
        if observed.intervals == [(1, observed.total_lines)]:
            return observed.fingerprint
        return None

    def record_complete(
        self,
        session_key: str,
        path: Path,
        fingerprint: FileFingerprint,
        *,
        total_lines: int,
    ) -> None:
        intervals = [] if total_lines == 0 else [(1, total_lines)]
        self._sessions.setdefault(session_key, {})[path] = _ObservedFile(
            fingerprint, total_lines, intervals
        )

    def invalidate(self, session_key: str, path: Path) -> None:
        self._sessions.get(session_key, {}).pop(path, None)


def execution_session_key(session_id: str | None, run_id: str | None) -> str:
    """匿名直接调用仍共享其显式上下文中的 tracker。"""

    return session_id or run_id or "__anonymous__"


def text_line_count(content: str) -> int:
    return len(content.splitlines())


def _merge(intervals: tuple[tuple[int, int], ...]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


__all__ = ["FileReadTracker", "execution_session_key", "text_line_count"]
