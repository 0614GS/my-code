"""Bounded project-session discovery for v3 transcripts."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nano_code.agent.contracts.session import SessionMetadata, SessionStart
from nano_code.messages import HumanMessage
from nano_code.sessions.codec import decode_entry
from nano_code.sessions.store import is_session_id

_MAX_HEAD_BYTES = 128 * 1024
_MAX_TAIL_BYTES = 128 * 1024
_MAX_TITLE_CHARS = 96


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    updated_at: datetime
    last_prompt: str | None = None
    created_at: datetime = datetime.min.replace(tzinfo=UTC)
    provider_id: str = ""
    model: str = ""


class SessionCatalog:
    def __init__(self, project_state_dir: Path) -> None:
        self.project_state_dir = project_state_dir

    def list(
        self, *, exclude_session_id: str | None = None, limit: int | None = None
    ) -> tuple[SessionSummary, ...]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        try:
            paths = tuple(self.project_state_dir.iterdir())
        except OSError:
            return ()
        summaries: list[SessionSummary] = []
        for path in paths:
            if (
                path.suffix != ".jsonl"
                or not is_session_id(path.stem)
                or path.stem == exclude_session_id
            ):
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                    continue
                summary = _read_summary(path)
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
            if summary is not None:
                summaries.append(summary)
        summaries.sort(
            key=lambda item: (item.updated_at, item.session_id), reverse=True
        )
        return tuple(summaries[:limit] if limit is not None else summaries)


def _read_summary(path: Path) -> SessionSummary | None:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(_MAX_HEAD_BYTES)
        tail_offset = max(0, size - _MAX_TAIL_BYTES)
        starts_at_boundary = True
        if tail_offset:
            handle.seek(tail_offset - 1)
            starts_at_boundary = handle.read(1) in {b"\n", b"\r"}
        handle.seek(tail_offset)
        tail = handle.read(_MAX_TAIL_BYTES)

    head_lines = head.splitlines()
    if not head_lines:
        return None
    start = decode_entry(json.loads(head_lines[0]))
    if not isinstance(start, SessionStart) or start.session_id != path.stem:
        return None

    first_prompt: str | None = None
    for line in head_lines[1:]:
        try:
            entry = decode_entry(json.loads(line))
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
            continue
        if isinstance(entry, HumanMessage) and entry.content.strip():
            first_prompt = entry.content
            break

    tail_lines = tail.splitlines()
    if tail_offset and not starts_at_boundary and tail_lines:
        tail_lines = tail_lines[1:]
    metadata: SessionMetadata | None = None
    for line in reversed(tail_lines):
        try:
            entry = decode_entry(json.loads(line))
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
            continue
        if isinstance(entry, SessionMetadata):
            metadata = entry
            break
    title_source = metadata.title if metadata and metadata.title else first_prompt
    if not title_source:
        return None
    created = _timestamp(metadata.created_at if metadata else start.created_at)
    updated = _timestamp(metadata.updated_at if metadata else start.created_at)
    return SessionSummary(
        session_id=path.stem,
        title=_truncate(" ".join(title_source.split())),
        last_prompt=metadata.last_prompt if metadata else first_prompt,
        created_at=created,
        updated_at=updated,
        provider_id=start.provider_id,
        model=start.model,
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Session timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _truncate(value: str) -> str:
    return value if len(value) <= _MAX_TITLE_CHARS else f"{value[:95]}…"
