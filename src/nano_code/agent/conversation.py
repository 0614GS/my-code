"""Agent 核心持有的 session 工作集与持久化事务边界。"""

from collections.abc import Iterable

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.session import (
    CompactBoundary,
    ContentReplacement,
    ConversationSnapshot,
    SessionSnapshot,
)
from nano_code.agent.ports.session import SessionRepository
from nano_code.messages import ChatMessage, ToolResultBlock, ToolUseBlock


class ConversationState:
    """维护一个 session 的活动历史、模型工作集和持久化优先不变量。

    所有会改变事实的操作都先调用 repository；只有写入成功后才刷新内存
    快照。这样磁盘写入失败不会让 Agent 继续使用一个不存在于 Transcript 的
    消息或压缩决策。
    """

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        self._snapshot = repository.snapshot()
        self._replace_snapshot(self._snapshot)
        self._repair_trailing_tool_uses()

    @property
    def repository(self) -> SessionRepository:
        return self._repository

    @property
    def session_id(self) -> str:
        return self._repository.session_id

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    @property
    def history(self) -> tuple[ChatMessage, ...]:
        return self._snapshot.history

    @property
    def working_messages(self) -> tuple[ChatMessage, ...]:
        return tuple(self._working_messages)

    @property
    def messages(self) -> list[ChatMessage]:
        """旧测试辅助 API；新的调用方应使用只读 ``working_messages``。"""

        return self._working_messages

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        return tuple(self._active_replacements.values())

    @property
    def compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        return self._snapshot.compact_boundaries

    @property
    def message_count(self) -> int:
        return len(self._working_messages)

    @property
    def history_message_count(self) -> int:
        return len(self._snapshot.history)

    @property
    def content_replacement_count(self) -> int:
        return len(self._active_replacements)

    @property
    def compact_count(self) -> int:
        return len(self._snapshot.compact_boundaries)

    def context_snapshot(self) -> ConversationSnapshot:
        """返回交给 ContextPort 的不可变工作集快照。"""

        return ConversationSnapshot(
            messages=tuple(self._working_messages),
            content_replacements=self.content_replacements,
        )

    def append(self, message: ChatMessage) -> None:
        """持久化优先追加消息，并在成功后刷新工作集。"""

        self._repository.append(message)
        self._refresh()

    def append_content_replacement(self, replacement: ContentReplacement) -> None:
        """持久化优先追加替换决策。"""

        self._repository.append_content_replacement(replacement)
        self._refresh()

    def append_tool_results(
        self,
        results: Iterable[ToolResultBlock],
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        """把同一模型响应的工具结果作为一条协议 user 消息追加。"""

        result_blocks = tuple(results)
        if not result_blocks:
            raise ValueError("A tool result message must contain at least one result")
        message = ChatMessage(
            role="user",
            origin="tool",
            content=result_blocks,
            parent_uuid=assistant_message.uuid,
            source_message_uuid=assistant_message.uuid,
        )
        self.append(message)
        return message

    def commit_compaction(self, outcome: CompactionOutcome) -> CompactBoundary:
        """按 replacement → boundary → summary 顺序提交压缩结果。"""

        for replacement in outcome.replacements:
            self._repository.append_content_replacement(replacement)
        self._repository.append_compact_boundary(outcome.boundary)
        self._repository.append(outcome.summary)
        self._refresh()
        return outcome.boundary

    def resume(self, repository: SessionRepository) -> tuple[ChatMessage, ...]:
        """先完整校验并修复目标会话，成功后原子替换当前状态。"""

        target_snapshot = repository.snapshot()
        if not target_snapshot.history:
            raise ValueError(f"Session contains no messages: {repository.session_id}")

        repairs = _trailing_tool_repairs(target_snapshot.history)
        if repairs is not None:
            repair = ChatMessage(
                role="user",
                origin="tool",
                content=repairs,
                parent_uuid=target_snapshot.history[-1].uuid,
                source_message_uuid=target_snapshot.history[-1].uuid,
            )
            repository.append(repair)
            target_snapshot = repository.snapshot()

        # 目标的所有 IO 和协议修复在这里已经完成；切换引用是最后一步。
        self._repository = repository
        self._replace_snapshot(target_snapshot)
        return target_snapshot.history

    def _refresh(self) -> None:
        self._replace_snapshot(self._repository.snapshot())

    def _replace_snapshot(self, snapshot: SessionSnapshot) -> None:
        self._snapshot = snapshot
        self._working_messages = list(snapshot.working_set)
        self._active_replacements = _active_replacements(
            snapshot.content_replacements,
            tuple(self._working_messages),
        )

    def _repair_trailing_tool_uses(self) -> None:
        repairs = _trailing_tool_repairs(self._snapshot.history)
        if repairs is None:
            return
        last = self._snapshot.history[-1]
        self.append(
            ChatMessage(
                role="user",
                origin="tool",
                content=repairs,
                parent_uuid=last.uuid,
                source_message_uuid=last.uuid,
            )
        )


def _trailing_tool_repairs(
    messages: tuple[ChatMessage, ...],
) -> tuple[ToolResultBlock, ...] | None:
    """为末尾未闭合的 assistant tool-use 构造协议错误结果。"""

    if not messages:
        return None
    trailing = messages[-1]
    if trailing.role != "assistant":
        return None
    calls = tuple(
        block for block in trailing.content if isinstance(block, ToolUseBlock)
    )
    if not calls:
        return None
    return tuple(
        ToolResultBlock(
            tool_use_id=call.id,
            content="Tool execution was interrupted before the session resumed.",
            is_error=True,
        )
        for call in calls
    )


def _active_replacements(
    replacements: tuple[ContentReplacement, ...],
    messages: tuple[ChatMessage, ...],
) -> dict[str, ContentReplacement]:
    tool_ids = {
        block.tool_use_id
        for message in messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    return {
        replacement.tool_use_id: replacement
        for replacement in replacements
        if replacement.tool_use_id in tool_ids
    }
