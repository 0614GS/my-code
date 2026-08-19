"""带父链校验的仅追加 JSONL 会话记录。"""

import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from nano_code.conversation.models import (
    AssistantMessage,
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ToolCall,
    ToolResultsMessage,
)
from nano_code.conversation.state import CompactBoundary, ContentReplacement
from nano_code.sessions.codec import (
    decode_entry,
    encode_boundary,
    encode_message,
    encode_metadata,
    encode_replacement,
    encode_start,
    encode_tool_presentation,
    presentations_from_json,
)
from nano_code.sessions.models import SessionMetadata, SessionSnapshot, SessionStart
from nano_code.sessions.records import ToolPresentationRecord
from nano_code.tools.presentation import ToolResultPresentation

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

__all__ = [
    "SessionStore",
    "is_session_id",
]


def is_session_id(value: str) -> bool:
    """会话 ID 是否为可安全映射到文件名的 UUID。"""

    return _UUID_PATTERN.fullmatch(value) is not None


class SessionStore:
    """在 Claude Code 风格的项目级目录中持久化一个会话。"""

    def __init__(
        self,
        project_state_dir: Path,
        session_id: str,
        *,
        start: SessionStart | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not is_session_id(session_id):
            raise ValueError("session_id must be a UUID")
        self._session_id = session_id
        self.project_state_dir = project_state_dir
        # 会话记录文件与 session 目录同处项目目录下；大型工具结果存放在后者的
        # ``tool-results/`` 子目录中。
        self.path = project_state_dir / f"{session_id}.jsonl"
        self.session_dir = project_state_dir / session_id
        self._clock = clock or (lambda: datetime.now(UTC))
        now = self._clock().isoformat()
        self._start = start or SessionStart(
            session_id,
            now,
            str(project_state_dir.resolve().parent),
            "anthropic",
            "unknown",
            "default",
            None,
            8192,
            160_000,
        )
        if self._start.session_id != session_id:
            raise ValueError("SessionStart session_id must match the store session_id")
        self._metadata: SessionMetadata | None = None

        # 这些索引只在显式 load 时从磁盘 hydration，之后随追加增量维护。
        # Session 只在打开时 hydration；之后这些索引随成功追加增量维护。
        self._known_ids: set[str] | None = None
        self._messages_by_id: dict[str, ConversationMessage] | None = None
        self._content_replacements: dict[str, ContentReplacement] | None = None
        self._boundaries: dict[str, CompactBoundary] | None = None
        self._tool_presentations: dict[str, ToolResultPresentation] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    def load(self) -> SessionSnapshot:
        """从 Transcript 完整恢复一次会话。

        这是初始化和显式 resume 边界，不是运行期 refresh API。
        """

        if not self.path.exists():
            self._known_ids = set()
            self._messages_by_id = {}
            self._content_replacements = {}
            self._boundaries = {}
            self._tool_presentations = {}
            return SessionSnapshot(history=(), working_set=())

        messages: list[ConversationMessage] = []
        by_id: dict[str, ConversationMessage] = {}
        seen: set[str] = set()
        replacements: dict[str, ContentReplacement] = {}
        boundaries: dict[str, CompactBoundary] = {}
        presentations: dict[str, ToolResultPresentation] = {}
        contents = self.path.read_bytes()
        lines = contents.splitlines(keepends=True)
        start: SessionStart | None = None
        metadata: SessionMetadata | None = None
        for line_number, encoded_line in enumerate(lines, start=1):
            terminated = encoded_line.endswith((b"\n", b"\r"))
            try:
                line = encoded_line.decode("utf-8")
            except UnicodeDecodeError as error:
                if line_number == len(lines) and not terminated:
                    break
                raise ValueError(
                    f"Invalid transcript line {line_number}: {error}"
                ) from error
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                legacy_version = (
                    raw.get("schema_version", raw.get("version"))
                    if isinstance(raw, dict)
                    else None
                )
                if (
                    line_number == 1
                    and isinstance(legacy_version, int)
                    and legacy_version != 5
                ):
                    raise ValueError(
                        f"Transcript schema v{legacy_version} is incompatible: "
                        f"{self.path}. "
                        "Start a new session to rebuild it."
                    )
                for tool_use_id, presentation in presentations_from_json(raw):
                    presentations[tool_use_id] = presentation
                entry = decode_entry(raw)
                if line_number == 1:
                    if not isinstance(entry, SessionStart):
                        raise ValueError(
                            "First transcript entry must be session_started"
                        )
                    if entry.session_id != self.session_id:
                        raise ValueError(
                            "session_started session_id does not match the filename"
                        )
                    start = entry
                    self._start = entry
                    continue
                if isinstance(entry, SessionStart):
                    raise ValueError("session_started may only be the first entry")
                if isinstance(entry, SessionMetadata):
                    if start is None or entry.created_at != start.created_at:
                        raise ValueError(
                            "session_metadata created_at must match session_started"
                        )
                    if datetime.fromisoformat(
                        entry.updated_at
                    ) < datetime.fromisoformat(entry.created_at):
                        raise ValueError(
                            "session_metadata updated_at cannot precede created_at"
                        )
                    metadata = entry
                    continue
                if isinstance(entry, ContentReplacement):
                    replacement = entry
                    previous = replacements.get(replacement.tool_use_id)
                    if previous is not None and previous != replacement:
                        raise ValueError(
                            "Conflicting content replacement for "
                            f"{replacement.tool_use_id}"
                        )
                    replacements[replacement.tool_use_id] = replacement
                    continue
                if isinstance(entry, CompactBoundary):
                    boundary = entry
                    previous_boundary = boundaries.get(boundary.id)
                    if previous_boundary is not None and previous_boundary != boundary:
                        raise ValueError(f"Conflicting compact boundary: {boundary.id}")
                    boundaries[boundary.id] = boundary
                    continue
                if isinstance(entry, ToolPresentationRecord):
                    presentations[entry.tool_use_id] = entry.presentation
                    continue
                message = entry
            except (json.JSONDecodeError, ValueError, TypeError) as error:
                if (
                    isinstance(error, json.JSONDecodeError)
                    and line_number == len(lines)
                    and not terminated
                ):
                    break
                raise ValueError(
                    f"Invalid transcript line {line_number}: {error}"
                ) from error
            if message.uuid in seen:
                raise ValueError(f"Duplicate message UUID: {message.uuid}")
            if not is_session_id(message.uuid):
                raise ValueError(f"Message UUID is invalid: {message.uuid}")
            if message.parent_uuid is not None and not is_session_id(
                message.parent_uuid
            ):
                raise ValueError(f"Parent UUID is invalid: {message.parent_uuid}")

            # 仅追加约束要求父节点先出现。拒绝悬空边比静默返回截断链更安全。
            if message.parent_uuid is not None and message.parent_uuid not in seen:
                raise ValueError(
                    f"Missing parent {message.parent_uuid} for {message.uuid}"
                )
            if isinstance(message, ToolResultsMessage):
                source = by_id.get(message.source_assistant_uuid)
                if (
                    message.parent_uuid != message.source_assistant_uuid
                    or not isinstance(source, AssistantMessage)
                ):
                    raise ValueError(
                        "Tool results must directly follow their source assistant"
                    )
                expected = {
                    block.id for block in source.content if isinstance(block, ToolCall)
                }
                actual = {block.tool_use_id for block in message.content}
                if actual != expected:
                    raise ValueError("Tool results do not match source tool calls")
            seen.add(message.uuid)
            messages.append(message)
            by_id[message.uuid] = message
        if start is None:
            raise ValueError(f"Transcript has no session_started entry: {self.path}")
        self._known_ids = seen
        self._messages_by_id = by_id
        self._content_replacements = replacements
        self._boundaries = boundaries
        self._tool_presentations = presentations
        self._metadata = metadata
        active = _active_parent_chain(messages, by_id)
        active_boundaries = _active_boundaries(boundaries, active)
        return SessionSnapshot(
            history=active,
            working_set=_working_set(active, active_boundaries),
            content_replacements=tuple(replacements.values()),
            compact_boundaries=active_boundaries,
            tool_presentations=tuple(presentations.items()),
            metadata=metadata,
        )

    def append(self, message: ConversationMessage) -> bool:
        """校验幂等性和父节点顺序后追加一条消息。"""

        return self.append_message(message)

    def append_message(
        self,
        message: ConversationMessage,
        presentations: tuple[tuple[str, ToolResultPresentation], ...] = (),
    ) -> bool:
        """Append a message and its optional presentation add-on records together."""

        self._ensure_loaded()
        assert self._known_ids is not None
        assert self._messages_by_id is not None
        if message.uuid in self._known_ids:
            if self._messages_by_id[message.uuid] != message:
                raise ValueError(f"Conflicting message UUID: {message.uuid}")
            return False
        if (
            message.parent_uuid is not None
            and message.parent_uuid not in self._known_ids
        ):
            raise ValueError(f"Unknown parent UUID: {message.parent_uuid}")
        if not is_session_id(message.uuid) or (
            message.parent_uuid is not None and not is_session_id(message.parent_uuid)
        ):
            raise ValueError("Message and parent UUIDs must be UUIDs")
        if isinstance(message, ToolResultsMessage):
            source = self._messages_by_id.get(message.source_assistant_uuid)
            if message.parent_uuid != message.source_assistant_uuid or not isinstance(
                source, AssistantMessage
            ):
                raise ValueError(
                    "Tool results must directly follow their source assistant"
                )
            expected = {
                block.id for block in source.content if isinstance(block, ToolCall)
            }
            actual = {block.tool_use_id for block in message.content}
            if actual != expected:
                raise ValueError("Tool results do not match source tool calls")

        if presentations and not isinstance(message, ToolResultsMessage):
            raise ValueError("Tool presentations require a tool-results message")
        result_ids = (
            {item.tool_use_id for item in message.content}
            if isinstance(message, ToolResultsMessage)
            else set()
        )
        if any(tool_use_id not in result_ids for tool_use_id, _ in presentations):
            raise ValueError("Tool presentation does not match the result message")
        records: list[object] = []
        if not self.path.exists():
            records.append(encode_start(self._start))
        records.extend(
            encode_tool_presentation(tool_use_id, presentation)
            for tool_use_id, presentation in presentations
        )
        records.append(encode_message(message))
        previous = self._metadata
        metadata = SessionMetadata(
            created_at=(previous.created_at if previous else self._start.created_at),
            updated_at=self._clock().isoformat(),
            title=previous.title if previous else None,
            last_prompt=(
                message.content
                if isinstance(message, HumanMessage)
                else previous.last_prompt
                if previous
                else None
            ),
        )
        records.append(encode_metadata(metadata))
        self._append_records(records)
        self._metadata = metadata

        # 仅在持久化写入成功后更新内存索引。
        self._known_ids.add(message.uuid)
        self._messages_by_id[message.uuid] = message
        assert self._tool_presentations is not None
        self._tool_presentations.update(presentations)
        return True

    def set_title(self, title: str) -> bool:
        """Append last-wins explicit display metadata."""

        normalized = title.strip()
        if not normalized:
            raise ValueError("Session title must not be empty")
        self._ensure_loaded()
        if not self.path.exists():
            raise ValueError("Cannot title a session before its first message")
        previous = self._metadata
        metadata = SessionMetadata(
            created_at=previous.created_at if previous else self._start.created_at,
            updated_at=self._clock().isoformat(),
            title=normalized,
            last_prompt=previous.last_prompt if previous else None,
        )
        if previous == metadata:
            return False
        self._append_records((encode_metadata(metadata),))
        self._metadata = metadata
        return True

    def append_content_replacement(self, replacement: ContentReplacement) -> bool:
        """幂等追加一次工具结果替换，不改写原始消息。"""

        self._ensure_loaded()
        assert self._content_replacements is not None
        previous = self._content_replacements.get(replacement.tool_use_id)
        if previous is not None:
            if previous != replacement:
                raise ValueError(
                    f"Conflicting content replacement: {replacement.tool_use_id}"
                )
            return False
        self._require_started()
        self._append_record(encode_replacement(replacement))
        self._content_replacements[replacement.tool_use_id] = replacement
        return True

    def append_compact_boundary(self, boundary: CompactBoundary) -> bool:
        """在 summary 前追加 compact 边界；不完整边界在恢复时自动忽略。"""

        self._ensure_loaded()
        assert self._known_ids is not None
        assert self._boundaries is not None
        if boundary.parent_uuid not in self._known_ids:
            raise ValueError(f"Unknown compact parent UUID: {boundary.parent_uuid}")
        previous = self._boundaries.get(boundary.id)
        if previous is not None:
            if previous != boundary:
                raise ValueError(f"Conflicting compact boundary: {boundary.id}")
            return False
        self._require_started()
        self._append_record(encode_boundary(boundary))
        self._boundaries[boundary.id] = boundary
        return True

    def append_compaction(
        self,
        replacements: tuple[ContentReplacement, ...],
        boundary: CompactBoundary,
        summary: ConversationSummaryMessage,
    ) -> None:
        """Persist one compaction decision in a single append operation."""

        self._ensure_loaded()
        assert self._known_ids is not None
        assert self._messages_by_id is not None
        assert self._content_replacements is not None
        assert self._boundaries is not None
        if boundary.parent_uuid not in self._known_ids:
            raise ValueError(f"Unknown compact parent UUID: {boundary.parent_uuid}")
        if summary.parent_uuid != boundary.parent_uuid:
            raise ValueError("Compact summary parent does not match boundary")
        if summary.uuid != boundary.summary_uuid:
            raise ValueError("Compact summary UUID does not match boundary")
        if summary.uuid in self._known_ids:
            if self._messages_by_id[summary.uuid] == summary:
                return
            raise ValueError(f"Conflicting message UUID: {summary.uuid}")
        pending_replacements = []
        for replacement in replacements:
            previous = self._content_replacements.get(replacement.tool_use_id)
            if previous is not None:
                if previous != replacement:
                    raise ValueError(
                        f"Conflicting content replacement: {replacement.tool_use_id}"
                    )
                continue
            pending_replacements.append(replacement)
        previous_boundary = self._boundaries.get(boundary.id)
        if previous_boundary is not None and previous_boundary != boundary:
            raise ValueError(f"Conflicting compact boundary: {boundary.id}")
        previous_metadata = self._metadata
        metadata = SessionMetadata(
            created_at=(
                previous_metadata.created_at
                if previous_metadata is not None
                else self._start.created_at
            ),
            updated_at=self._clock().isoformat(),
            title=previous_metadata.title if previous_metadata is not None else None,
            last_prompt=(
                previous_metadata.last_prompt if previous_metadata is not None else None
            ),
        )
        records = [encode_replacement(item) for item in pending_replacements]
        if previous_boundary is None:
            records.append(encode_boundary(boundary))
        records.extend((encode_message(summary), encode_metadata(metadata)))
        self._append_records(records)
        self._content_replacements.update(
            (item.tool_use_id, item) for item in pending_replacements
        )
        self._boundaries[boundary.id] = boundary
        self._known_ids.add(summary.uuid)
        self._messages_by_id[summary.uuid] = summary
        self._metadata = metadata

    def _ensure_loaded(self) -> None:
        if self._known_ids is None:
            self.load()

    def _require_started(self) -> None:
        if not self.path.exists():
            raise ValueError("Session must contain a message before auxiliary records")

    def _append_record(self, record: object) -> None:
        self._append_records((record,))

    def _append_records(self, records: Iterable[object]) -> None:
        """以仅属主可访问权限追加一个自包含 JSONL 记录。"""

        # 会话数据可能包含源码和命令输出，因此目录和文件都使用仅属主可访问的权限。
        self.project_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.project_state_dir, 0o700)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            # 每行一个自包含 JSON 值，使写入中断只影响可诊断的末条记录，
            # 而不会损坏整个文件。
            for record in records:
                json.dump(record, handle, ensure_ascii=False)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _active_parent_chain(
    messages: list[ConversationMessage], by_id: dict[str, ConversationMessage]
) -> tuple[ConversationMessage, ...]:
    """从最后一条记录沿父指针恢复当前活动分支。"""

    if not messages:
        return ()
    chain: list[ConversationMessage] = []
    current: ConversationMessage | None = messages[-1]
    while current is not None:
        chain.append(current)
        current = (
            None if current.parent_uuid is None else by_id.get(current.parent_uuid)
        )
    chain.reverse()
    return tuple(chain)


def _active_boundaries(
    boundaries: dict[str, CompactBoundary],
    history: tuple[ConversationMessage, ...],
) -> tuple[CompactBoundary, ...]:
    """返回父节点和 summary 都位于活动链上的完整边界。"""

    active_ids = {message.uuid for message in history}
    return tuple(
        boundary
        for boundary in boundaries.values()
        if boundary.parent_uuid in active_ids and boundary.summary_uuid in active_ids
    )


def _working_set(
    history: tuple[ConversationMessage, ...],
    boundaries: tuple[CompactBoundary, ...],
) -> tuple[ConversationMessage, ...]:
    """从最后一个有效 compact summary 开始构造模型工作集。"""

    if not boundaries:
        return history
    summary_uuid = boundaries[-1].summary_uuid
    for index, message in enumerate(history):
        if message.uuid == summary_uuid:
            return history[index:]
    return history
