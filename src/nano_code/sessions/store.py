"""带父链校验的仅追加 JSONL 会话记录。"""

import json
import os
import re
from pathlib import Path

from nano_code.messages import ChatMessage
from nano_code.messages.codec import message_from_json, message_to_json

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_session_id(value: str) -> bool:
    """会话 ID 是否为可安全映射到文件名的 UUID。"""

    return _UUID_PATTERN.fullmatch(value) is not None


class SessionStore:
    """在 Claude Code 风格的项目级目录中持久化一个会话。"""

    def __init__(self, project_state_dir: Path, session_id: str) -> None:
        if not is_session_id(session_id):
            raise ValueError("session_id must be a UUID")
        self.session_id = session_id
        self.project_state_dir = project_state_dir
        # 会话记录文件与 session 目录同处项目目录下；大型工具结果存放在后者的
        # ``tool-results/`` 子目录中。
        self.path = project_state_dir / f"{session_id}.jsonl"
        self.session_dir = project_state_dir / session_id

        # 该集合既是幂等保护，也是低成本的父节点存在性索引。
        # 它从磁盘初始化，使恢复的存储与新建存储行为一致。
        self._known_ids: set[str] | None = None

    def load(self) -> tuple[ChatMessage, ...]:
        if not self.path.exists():
            self._known_ids = set()
            return ()

        messages: list[ChatMessage] = []
        by_id: dict[str, ChatMessage] = {}
        seen: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
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
        return _active_parent_chain(messages, by_id)

    def append(self, message: ChatMessage) -> None:
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
            json.dump(message_to_json(message), handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        # 仅在持久化写入成功后更新内存索引。
        self._known_ids.add(message.uuid)


def _active_parent_chain(
    messages: list[ChatMessage], by_id: dict[str, ChatMessage]
) -> tuple[ChatMessage, ...]:
    """从最后一条记录沿父指针恢复当前活动分支。"""

    if not messages:
        return ()
    chain: list[ChatMessage] = []
    current: ChatMessage | None = messages[-1]
    while current is not None:
        chain.append(current)
        current = (
            None if current.parent_uuid is None else by_id.get(current.parent_uuid)
        )
    chain.reverse()
    return tuple(chain)
