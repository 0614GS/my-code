"""模型文件观察授权与进程内近期工作集。"""

from dataclasses import dataclass, field
from pathlib import Path

from my_code.workspace.local import FileFingerprint


@dataclass(slots=True)
class _ObservedFile:
    fingerprint: FileFingerprint
    total_lines: int
    observed: bool = False
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
            observed.observed = True
            return
        if start is None or end is None:
            return
        observed.observed = True
        if not complete_lines:
            return
        observed.intervals = _merge((*observed.intervals, (start, end)))

    def require_observed(self, session_key: str, path: Path) -> FileFingerprint | None:
        """返回模型已实际看到内容的当前文件版本。"""

        observed = self._sessions.get(session_key, {}).get(path)
        if observed is None or not observed.observed:
            return None
        return observed.fingerprint

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
            fingerprint, total_lines, True, intervals
        )

    def invalidate(self, session_key: str, path: Path) -> None:
        self._sessions.get(session_key, {}).pop(path, None)

    def clear_session(self, session_key: str) -> None:
        """使一次 compact 前的全部文件观察授权失效。"""

        self._sessions.pop(session_key, None)

    def replace_session_observations(
        self,
        session_key: str,
        files: tuple[tuple[Path, FileFingerprint, int, bool], ...],
    ) -> None:
        """一次替换 compact 后重新校验的观察状态。"""

        if not files:
            self._sessions.pop(session_key, None)
            return
        self._sessions[session_key] = {
            path: _ObservedFile(
                fingerprint,
                total_lines,
                True,
                [] if total_lines == 0 or not complete else [(1, total_lines)],
            )
            for path, fingerprint, total_lines, complete in files
        }

    def clear(self) -> None:
        self._sessions.clear()


@dataclass(frozen=True, slots=True)
class RecentFile:
    """近期访问的规范化路径及当时成功操作所得版本。"""

    path: Path
    fingerprint: FileFingerprint
    sequence: int


class RecentFileRegistry:
    """按 Session 隔离的有界 LRU，只保存元数据而不缓存正文。"""

    def __init__(self, *, max_files_per_session: int = 100) -> None:
        if max_files_per_session < 1:
            raise ValueError("Recent file registry limit must be positive")
        self._max_files_per_session = max_files_per_session
        self._sessions: dict[str, dict[Path, RecentFile]] = {}
        self._sequence = 0

    def record(
        self,
        session_key: str,
        path: Path,
        fingerprint: FileFingerprint,
    ) -> None:
        self._sequence += 1
        files = self._sessions.setdefault(session_key, {})
        files[path] = RecentFile(path, fingerprint, self._sequence)
        overflow = len(files) - self._max_files_per_session
        if overflow > 0:
            oldest = sorted(files.values(), key=lambda item: item.sequence)[:overflow]
            for item in oldest:
                files.pop(item.path, None)

    def recent(self, session_key: str) -> tuple[RecentFile, ...]:
        return tuple(
            sorted(
                self._sessions.get(session_key, {}).values(),
                key=lambda item: item.sequence,
                reverse=True,
            )
        )

    def clear(self) -> None:
        """显式释放运行期状态；持久化恢复不会重建这些条目。"""

        self._sessions.clear()


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


__all__ = [
    "FileReadTracker",
    "RecentFile",
    "RecentFileRegistry",
    "execution_session_key",
    "text_line_count",
]
