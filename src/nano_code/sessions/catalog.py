"""项目级会话发现与轻量预览。"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nano_code.messages import HumanMessage
from nano_code.sessions.codec import decode_entry
from nano_code.sessions.store import is_session_id

_MAX_PREVIEW_BYTES = 128 * 1024
_MAX_TITLE_CHARS = 96


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """无需完整加载会话即可展示的安全元数据。"""

    session_id: str
    title: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _SessionCandidate:
    session_id: str
    path: Path
    updated_at: datetime


class SessionCatalog:
    """只发现一个项目状态目录中的可恢复会话。"""

    def __init__(self, project_state_dir: Path) -> None:
        self.project_state_dir = project_state_dir

    def list(
        self,
        *,
        exclude_session_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[SessionSummary, ...]:
        """通过 stat 和文件头构建列表，不解析完整 JSONL。"""

        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        try:
            paths = tuple(self.project_state_dir.iterdir())
        except OSError:
            return ()

        candidates: list[_SessionCandidate] = []
        for path in paths:
            if path.suffix != ".jsonl" or not is_session_id(path.stem):
                continue
            if path.stem == exclude_session_id:
                continue
            try:
                if path.is_symlink():
                    continue
                metadata = path.stat()
                if not path.is_file() or metadata.st_size == 0:
                    continue
            except OSError:
                continue
            candidates.append(
                _SessionCandidate(
                    session_id=path.stem,
                    path=path,
                    updated_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
                )
            )
        candidates.sort(
            key=lambda item: (item.updated_at, item.session_id), reverse=True
        )

        summaries: list[SessionSummary] = []
        for candidate in candidates:
            try:
                title = _read_first_prompt(candidate.path)
            except OSError:
                continue
            if title is None:
                continue
            summaries.append(
                SessionSummary(
                    session_id=candidate.session_id,
                    title=title,
                    updated_at=candidate.updated_at,
                )
            )
            if limit is not None and len(summaries) >= limit:
                break
        return tuple(summaries)


def _read_first_prompt(path: Path) -> str | None:
    """在有界文件头中寻找首条用户文本；异常首记录不进入选择器。"""

    consumed = 0
    is_first_record = True
    with path.open("rb") as handle:
        while consumed < _MAX_PREVIEW_BYTES:
            remaining = _MAX_PREVIEW_BYTES - consumed
            line = handle.readline(remaining + 1)
            if not line:
                break
            consumed += len(line)
            if len(line) > remaining:
                break
            if not line.strip():
                continue
            try:
                message = decode_entry(json.loads(line))
            except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
                if is_first_record:
                    return None
                continue
            is_first_record = False
            if not isinstance(message, HumanMessage):
                continue
            normalized = " ".join(message.content.split())
            if normalized:
                return _truncate_title(normalized)

    return None


def _truncate_title(title: str) -> str:
    if len(title) <= _MAX_TITLE_CHARS:
        return title
    return f"{title[: _MAX_TITLE_CHARS - 1]}…"
