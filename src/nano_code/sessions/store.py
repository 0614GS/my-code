"""带父链校验的仅追加 JSONL 会话记录。"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from nano_code.agent.contracts.session import (
    CompactBoundary,
    CompactTrigger,
    ContentReplacement,
    SessionSnapshot,
)
from nano_code.agent.ports.session import SessionRepository
from nano_code.messages import TranscriptMessage
from nano_code.messages.codec import message_from_json, message_to_json

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

__all__ = ["SessionStore", "is_session_id"]


def is_session_id(value: str) -> bool:
    """会话 ID 是否为可安全映射到文件名的 UUID。"""

    return _UUID_PATTERN.fullmatch(value) is not None


class SessionStore(SessionRepository):
    """在 Claude Code 风格的项目级目录中持久化一个会话。"""

    def __init__(self, project_state_dir: Path, session_id: str) -> None:
        if not is_session_id(session_id):
            raise ValueError("session_id must be a UUID")
        self._session_id = session_id
        self.project_state_dir = project_state_dir
        # 会话记录文件与 session 目录同处项目目录下；大型工具结果存放在后者的
        # ``tool-results/`` 子目录中。
        self.path = project_state_dir / f"{session_id}.jsonl"
        self.session_dir = project_state_dir / session_id

        # 该集合既是幂等保护，也是低成本的父节点存在性索引。
        # 它从磁盘初始化，使恢复的存储与新建存储行为一致。
        self._known_ids: set[str] | None = None
        self._content_replacements: dict[str, ContentReplacement] | None = None
        self._compact_boundaries: dict[str, CompactBoundary] | None = None
        self._pending_compact_boundaries: dict[str, CompactBoundary] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    def load(self) -> tuple[TranscriptMessage, ...]:
        if not self.path.exists():
            self._known_ids = set()
            self._content_replacements = {}
            self._compact_boundaries = {}
            self._pending_compact_boundaries = {}
            return ()

        messages: list[TranscriptMessage] = []
        by_id: dict[str, TranscriptMessage] = {}
        seen: set[str] = set()
        replacements: dict[str, ContentReplacement] = {}
        boundaries: dict[str, CompactBoundary] = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if (
                        isinstance(raw, dict)
                        and raw.get("type") == "content_replacement"
                    ):
                        replacement = _replacement_from_json(raw)
                        previous = replacements.get(replacement.tool_use_id)
                        if previous is not None and previous != replacement:
                            raise ValueError(
                                "Conflicting content replacement for "
                                f"{replacement.tool_use_id}"
                            )
                        replacements[replacement.tool_use_id] = replacement
                        continue
                    if isinstance(raw, dict) and raw.get("type") == "compact_boundary":
                        boundary = _boundary_from_json(raw)
                        previous_boundary = boundaries.get(boundary.id)
                        if (
                            previous_boundary is not None
                            and previous_boundary != boundary
                        ):
                            raise ValueError(
                                f"Conflicting compact boundary: {boundary.id}"
                            )
                        boundaries[boundary.id] = boundary
                        continue
                    message = message_from_json(raw)
                except (json.JSONDecodeError, ValueError, TypeError) as error:
                    raise ValueError(
                        f"Invalid transcript line {line_number}: {error}"
                    ) from error
                if message.uuid in seen:
                    raise ValueError(f"Duplicate message UUID: {message.uuid}")

                # 仅追加约束要求父节点先出现。拒绝悬空边比静默返回截断链更安全。
                if message.parent_uuid is not None and message.parent_uuid not in seen:
                    raise ValueError(
                        f"Missing parent {message.parent_uuid} for {message.uuid}"
                    )
                seen.add(message.uuid)
                messages.append(message)
                by_id[message.uuid] = message
        self._known_ids = seen
        self._content_replacements = replacements
        active = _active_parent_chain(messages, by_id)
        active_ids = {message.uuid for message in active}
        # 进程可能在 boundary 与 summary 两次追加之间退出；缺少 summary 的
        # 边界不生效，原始消息链仍可安全恢复。
        self._compact_boundaries = {
            boundary_id: boundary
            for boundary_id, boundary in boundaries.items()
            if boundary.parent_uuid in active_ids
            and boundary.summary_uuid in active_ids
        }
        self._pending_compact_boundaries = {}
        return active

    def snapshot(self) -> SessionSnapshot:
        """一次读取会话历史、工作集和结构化上下文记录。"""

        history = self.load()
        return SessionSnapshot(
            history=history,
            working_set=self.load_working_set(history),
            content_replacements=self.load_content_replacements(),
            compact_boundaries=self.load_compact_boundaries(),
        )

    def load_content_replacements(self) -> tuple[ContentReplacement, ...]:
        """返回按首次写入顺序排列的稳定模型输入规范化决策。"""

        if self._content_replacements is None:
            self.load()
        assert self._content_replacements is not None
        return tuple(self._content_replacements.values())

    def load_compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        if self._compact_boundaries is None:
            self.load()
        assert self._compact_boundaries is not None
        return tuple(self._compact_boundaries.values())

    def load_working_set(
        self, messages: tuple[TranscriptMessage, ...] | None = None
    ) -> tuple[TranscriptMessage, ...]:
        """返回最后一个有效 compact summary 开始的活动工作集。"""

        active = self.load() if messages is None else messages
        boundaries = self.load_compact_boundaries()
        if not boundaries:
            return active
        summary_uuid = boundaries[-1].summary_uuid
        for index, message in enumerate(active):
            if message.uuid == summary_uuid:
                return active[index:]
        return active

    def append(self, message: TranscriptMessage) -> None:
        """校验幂等性和父节点顺序后追加一条消息。"""

        if self._known_ids is None:
            self.load()
        assert self._known_ids is not None
        if message.uuid in self._known_ids:
            # 智能体迭代可能重复访问同一内存前缀。基于 UUID 的幂等性
            # 可防止每次迭代重复追加。
            return
        if (
            message.parent_uuid is not None
            and message.parent_uuid not in self._known_ids
        ):
            raise ValueError(f"Unknown parent UUID: {message.parent_uuid}")

        self._append_record(message_to_json(message))

        # 仅在持久化写入成功后更新内存索引。
        self._known_ids.add(message.uuid)
        self._activate_pending_boundaries()

    def append_content_replacement(self, replacement: ContentReplacement) -> None:
        """幂等追加一次工具结果替换，不改写原始消息。"""

        if self._content_replacements is None:
            self.load()
        assert self._content_replacements is not None
        previous = self._content_replacements.get(replacement.tool_use_id)
        if previous is not None:
            if previous != replacement:
                raise ValueError(
                    f"Conflicting content replacement: {replacement.tool_use_id}"
                )
            return
        self._append_record(
            {
                "type": "content_replacement",
                "version": 1,
                "tool_use_id": replacement.tool_use_id,
                "tool_name": replacement.tool_name,
                "original_chars": replacement.original_chars,
                "content": replacement.content,
            }
        )
        self._content_replacements[replacement.tool_use_id] = replacement

    def append_compact_boundary(self, boundary: CompactBoundary) -> None:
        """在 summary 前追加 compact 边界；不完整边界在恢复时自动忽略。"""

        if self._known_ids is None or self._compact_boundaries is None:
            self.load()
        assert self._known_ids is not None
        assert self._compact_boundaries is not None
        if self._pending_compact_boundaries is None:
            self._pending_compact_boundaries = {}
        if boundary.parent_uuid not in self._known_ids:
            raise ValueError(f"Unknown compact parent UUID: {boundary.parent_uuid}")
        previous = self._compact_boundaries.get(boundary.id)
        if previous is None:
            previous = self._pending_compact_boundaries.get(boundary.id)
        if previous is not None:
            if previous != boundary:
                raise ValueError(f"Conflicting compact boundary: {boundary.id}")
            return
        self._append_record(
            {
                "type": "compact_boundary",
                "version": 1,
                "id": boundary.id,
                "parent_uuid": boundary.parent_uuid,
                "summary_uuid": boundary.summary_uuid,
                "trigger": boundary.trigger,
                "pre_compact_chars": boundary.pre_compact_chars,
            }
        )
        self._pending_compact_boundaries[boundary.id] = boundary
        self._activate_pending_boundaries()

    def _activate_pending_boundaries(self) -> None:
        """仅在 boundary 指向的 summary 已成功写入后使其生效。"""

        if self._known_ids is None or self._compact_boundaries is None:
            return
        if self._pending_compact_boundaries is None:
            return
        for boundary_id, boundary in tuple(self._pending_compact_boundaries.items()):
            if (
                boundary.parent_uuid in self._known_ids
                and boundary.summary_uuid in self._known_ids
            ):
                self._compact_boundaries[boundary_id] = boundary
                del self._pending_compact_boundaries[boundary_id]

    def _append_record(self, record: object) -> None:
        """以仅属主可访问权限追加一个自包含 JSONL 记录。"""

        # 会话数据可能包含源码和命令输出，因此目录和文件都使用仅属主可访问的权限。
        self.project_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            # 每行一个自包含 JSON 值，使写入中断只影响可诊断的末条记录，
            # 而不会损坏整个文件。
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _replacement_from_json(value: Mapping[str, object]) -> ContentReplacement:
    """严格解析内容替换记录。"""

    if value.get("version") != 1:
        raise ValueError("Unsupported content replacement record")
    tool_use_id = value.get("tool_use_id")
    tool_name = value.get("tool_name")
    original_chars = value.get("original_chars")
    content = value.get("content")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise ValueError("Content replacement tool_use_id must be a string")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("Content replacement tool_name must be a string")
    if (
        not isinstance(original_chars, int)
        or isinstance(original_chars, bool)
        or original_chars < 1
    ):
        raise ValueError("Content replacement original_chars must be positive")
    if not isinstance(content, str) or not content:
        raise ValueError("Content replacement content must be a string")
    return ContentReplacement(tool_use_id, tool_name, original_chars, content)


def _boundary_from_json(value: Mapping[str, object]) -> CompactBoundary:
    if value.get("version") != 1:
        raise ValueError("Unsupported compact boundary record")
    boundary_id = value.get("id")
    parent_uuid = value.get("parent_uuid")
    summary_uuid = value.get("summary_uuid")
    trigger = value.get("trigger")
    pre_compact_chars = value.get("pre_compact_chars")
    if not isinstance(boundary_id, str) or not boundary_id:
        raise ValueError("Compact boundary id must be a string")
    if not isinstance(parent_uuid, str) or not parent_uuid:
        raise ValueError("Compact boundary parent_uuid must be a string")
    if not isinstance(summary_uuid, str) or not summary_uuid:
        raise ValueError("Compact boundary summary_uuid must be a string")
    if trigger == "auto":
        actual_trigger: CompactTrigger = "auto"
    elif trigger == "manual":
        actual_trigger = "manual"
    elif trigger == "reactive":
        actual_trigger = "reactive"
    else:
        raise ValueError("Unsupported compact trigger")
    if (
        not isinstance(pre_compact_chars, int)
        or isinstance(pre_compact_chars, bool)
        or pre_compact_chars < 1
    ):
        raise ValueError("Compact boundary pre_compact_chars must be positive")
    return CompactBoundary(
        id=boundary_id,
        parent_uuid=parent_uuid,
        summary_uuid=summary_uuid,
        trigger=actual_trigger,
        pre_compact_chars=pre_compact_chars,
    )


def _active_parent_chain(
    messages: list[TranscriptMessage], by_id: dict[str, TranscriptMessage]
) -> tuple[TranscriptMessage, ...]:
    """从最后一条记录沿父指针恢复当前活动分支。"""

    if not messages:
        return ()
    chain: list[TranscriptMessage] = []
    current: TranscriptMessage | None = messages[-1]
    while current is not None:
        chain.append(current)
        current = (
            None if current.parent_uuid is None else by_id.get(current.parent_uuid)
        )
    chain.reverse()
    return tuple(chain)
