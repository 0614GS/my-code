"""只读查看持久化证据，不执行恢复时的工具配对修复。"""

from dataclasses import dataclass, replace
from pathlib import Path

from my_code.conversation.models import ConversationEntry
from my_code.sessions._request_audit import RequestAuditStore
from my_code.sessions._store import SessionStore
from my_code.sessions.models import InvocationHistoryEntry, SessionStart
from my_code.sessions.request_audit import RequestAuditSnapshot


@dataclass(frozen=True, slots=True)
class SessionInspection:
    start: SessionStart
    conversation: tuple[ConversationEntry, ...]
    invocations: tuple[InvocationHistoryEntry, ...]
    audit: RequestAuditSnapshot


def inspect_session(project_state_dir: Path, session_id: str) -> SessionInspection:
    """读取已存在且停止写入的会话；缺失或损坏时明确失败，不创建、修复文件。"""

    store = SessionStore(project_state_dir, session_id)
    if not store.path.is_file():
        raise FileNotFoundError(store.path)
    loaded = store.load()
    audit = RequestAuditStore(store.session_dir).snapshot()
    return SessionInspection(
        store.start,
        loaded.conversation,
        loaded.invocation_history,
        replace(
            audit, legacy_missing=bool(loaded.conversation) and audit.legacy_missing
        ),
    )


__all__ = ["SessionInspection", "inspect_session"]
