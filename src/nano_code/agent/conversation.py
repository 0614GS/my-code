"""Agent 核心持有的 session 工作集与持久化事务边界。"""

from collections.abc import Iterable

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.session import (
    CompactBoundary,
    ContentReplacement,
    ConversationSnapshot,
    DeliveredContextAttachment,
    SessionSnapshot,
)
from nano_code.agent.ports.session import SessionRepository
from nano_code.messages import (
    AssistantMessage,
    ContextAttachment,
    ConversationMessage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)


class ConversationState:
    """维护一个 session 的活动历史、模型工作集和持久化优先不变量。

    repository 只在初始化或 resume 时 hydration 一次。运行期的事实来源
    是这个对象持有的内存状态；所有变更仍先持久化，成功后再直接应用
    同一个领域事实，不重新读取 Transcript。
    """

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        self._snapshot: SessionSnapshot
        self._all_replacements: dict[str, ContentReplacement]
        self._all_boundaries: dict[str, CompactBoundary]
        self._active_replacements: dict[str, ContentReplacement]
        self._runtime_attachments: tuple[DeliveredContextAttachment, ...] = ()
        self._replace_snapshot(repository.load())
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
    def history(self) -> tuple[ConversationMessage, ...]:
        return self._snapshot.history

    @property
    def working_messages(self) -> tuple[ConversationMessage, ...]:
        return self._snapshot.working_set

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        return tuple(self._active_replacements.values())

    @property
    def compact_boundaries(self) -> tuple[CompactBoundary, ...]:
        return self._snapshot.compact_boundaries

    @property
    def message_count(self) -> int:
        return len(self._snapshot.working_set)

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

        working_ids = {message.uuid for message in self._snapshot.working_set}
        return ConversationSnapshot(
            messages=self._snapshot.working_set,
            content_replacements=self.content_replacements,
            session_history=self._snapshot.history,
            runtime_attachments=tuple(
                delivery
                for delivery in self._runtime_attachments
                if delivery.after_message_uuid in working_ids
            ),
        )

    def append_runtime_attachments(
        self, attachments: tuple[ContextAttachment, ...]
    ) -> None:
        """把已发送 attachment 留在本次进程的会话历史中，不写 Transcript。"""

        if not attachments:
            return
        if not self._snapshot.history:
            raise ValueError("Runtime attachments require a conversation anchor")
        anchor = self._snapshot.history[-1].uuid
        self._runtime_attachments += tuple(
            DeliveredContextAttachment(anchor, attachment) for attachment in attachments
        )

    def append(self, message: ConversationMessage) -> None:
        """持久化优先追加消息，成功后增量更新运行时状态。"""

        history = _history_after_append(self._snapshot.history, message)
        if history is self._snapshot.history:
            return
        if not self._repository.append(message):
            raise ValueError(
                f"Message UUID already exists outside the active conversation: "
                f"{message.uuid}"
            )
        self._rebuild_snapshot(history)

    def append_content_replacement(self, replacement: ContentReplacement) -> None:
        """持久化优先追加替换决策。"""

        previous = self._all_replacements.get(replacement.tool_use_id)
        if previous is not None:
            if previous != replacement:
                raise ValueError(
                    f"Conflicting content replacement: {replacement.tool_use_id}"
                )
            return
        self._repository.append_content_replacement(replacement)
        self._all_replacements[replacement.tool_use_id] = replacement
        self._rebuild_snapshot(self._snapshot.history)

    def append_tool_results(
        self,
        results: Iterable[ToolResult],
        assistant_message: AssistantMessage,
    ) -> ToolResultsMessage:
        """把同一模型响应的工具结果作为一条协议 user 消息追加。"""

        result_blocks = tuple(results)
        if not result_blocks:
            raise ValueError("A tool result message must contain at least one result")
        message = ToolResultsMessage(
            content=result_blocks,
            parent_uuid=assistant_message.uuid,
            source_assistant_uuid=assistant_message.uuid,
        )
        self.append(message)
        return message

    def commit_compaction(self, outcome: CompactionOutcome) -> CompactBoundary:
        """按 replacement → boundary → summary 顺序提交压缩结果。"""

        history = _history_after_append(self._snapshot.history, outcome.summary)
        replacements = dict(self._all_replacements)
        for replacement in outcome.replacements:
            previous = replacements.get(replacement.tool_use_id)
            if previous is not None and previous != replacement:
                raise ValueError(
                    f"Conflicting content replacement: {replacement.tool_use_id}"
                )
            replacements[replacement.tool_use_id] = replacement
        previous_boundary = self._all_boundaries.get(outcome.boundary.id)
        if previous_boundary is not None and previous_boundary != outcome.boundary:
            raise ValueError(f"Conflicting compact boundary: {outcome.boundary.id}")

        for replacement in outcome.replacements:
            self._repository.append_content_replacement(replacement)
        self._repository.append_compact_boundary(outcome.boundary)
        summary_appended = self._repository.append(outcome.summary)
        if not summary_appended and history is not self._snapshot.history:
            raise ValueError(
                f"Summary UUID already exists outside the active conversation: "
                f"{outcome.summary.uuid}"
            )

        self._all_replacements = replacements
        self._all_boundaries[outcome.boundary.id] = outcome.boundary
        self._rebuild_snapshot(history)
        return outcome.boundary

    def resume(self, repository: SessionRepository) -> tuple[ConversationMessage, ...]:
        """先完整校验并修复目标会话，成功后原子替换当前状态。"""

        target_snapshot = repository.load()
        if not target_snapshot.history:
            raise ValueError(f"Session contains no messages: {repository.session_id}")

        repairs = _trailing_tool_repairs(target_snapshot.history)
        if repairs is not None:
            repair = ToolResultsMessage(
                content=repairs,
                parent_uuid=target_snapshot.history[-1].uuid,
                source_assistant_uuid=target_snapshot.history[-1].uuid,
            )
            history = _history_after_append(target_snapshot.history, repair)
            if not repository.append(repair):
                raise ValueError(
                    f"Repair UUID already exists outside the active conversation: "
                    f"{repair.uuid}"
                )
            target_snapshot = _snapshot_for_history(target_snapshot, history)

        # 目标的所有 IO 和协议修复在这里已经完成；切换引用是最后一步。
        self._repository = repository
        self._replace_snapshot(target_snapshot)
        # CC 的普通 attachment 不写 Transcript；切换或恢复 session 时不存在可重放事实。
        self._runtime_attachments = ()
        return target_snapshot.history

    def _replace_snapshot(self, snapshot: SessionSnapshot) -> None:
        self._snapshot = snapshot
        self._all_replacements = {
            replacement.tool_use_id: replacement
            for replacement in snapshot.content_replacements
        }
        self._all_boundaries = {
            boundary.id: boundary for boundary in snapshot.compact_boundaries
        }
        self._active_replacements = _active_replacements(
            snapshot.content_replacements,
            snapshot.working_set,
        )

    def _rebuild_snapshot(self, history: tuple[ConversationMessage, ...]) -> None:
        self._replace_snapshot(
            _snapshot_for_history(
                SessionSnapshot(
                    history=history,
                    working_set=history,
                    content_replacements=tuple(self._all_replacements.values()),
                    compact_boundaries=tuple(self._all_boundaries.values()),
                    metadata=self._snapshot.metadata,
                ),
                history,
            )
        )

    def _repair_trailing_tool_uses(self) -> None:
        repairs = _trailing_tool_repairs(self._snapshot.history)
        if repairs is None:
            return
        last = self._snapshot.history[-1]
        self.append(
            ToolResultsMessage(
                content=repairs,
                parent_uuid=last.uuid,
                source_assistant_uuid=last.uuid,
            )
        )


def _trailing_tool_repairs(
    messages: tuple[ConversationMessage, ...],
) -> tuple[ToolResult, ...] | None:
    """为末尾未闭合的 assistant tool-use 构造协议错误结果。"""

    if not messages:
        return None
    trailing = messages[-1]
    if not isinstance(trailing, AssistantMessage):
        return None
    calls = tuple(block for block in trailing.content if isinstance(block, ToolCall))
    if not calls:
        return None
    return tuple(
        ToolResult(
            tool_use_id=call.id,
            content="Tool execution was interrupted before the session resumed.",
            is_error=True,
        )
        for call in calls
    )


def _active_replacements(
    replacements: tuple[ContentReplacement, ...],
    messages: tuple[ConversationMessage, ...],
) -> dict[str, ContentReplacement]:
    tool_ids = {
        block.tool_use_id
        for message in messages
        if isinstance(message, ToolResultsMessage)
        for block in message.content
        if isinstance(block, ToolResult)
    }
    return {
        replacement.tool_use_id: replacement
        for replacement in replacements
        if replacement.tool_use_id in tool_ids
    }


def _history_after_append(
    history: tuple[ConversationMessage, ...], message: ConversationMessage
) -> tuple[ConversationMessage, ...]:
    """在当前活动链上应用一次追加或分支。"""

    for existing in history:
        if existing.uuid != message.uuid:
            continue
        if existing != message:
            raise ValueError(f"Conflicting message UUID: {message.uuid}")
        return history
    if message.parent_uuid is None:
        return (message,)
    for index, existing in enumerate(history):
        if existing.uuid == message.parent_uuid:
            return history[: index + 1] + (message,)
    raise ValueError(f"Parent is not in the active conversation: {message.parent_uuid}")


def _snapshot_for_history(
    snapshot: SessionSnapshot,
    history: tuple[ConversationMessage, ...],
) -> SessionSnapshot:
    """从运行时活动链确定有效 boundary 和 compact 工作集。"""

    active_ids = {message.uuid for message in history}
    boundaries = tuple(
        boundary
        for boundary in snapshot.compact_boundaries
        if boundary.parent_uuid in active_ids and boundary.summary_uuid in active_ids
    )
    working_set = history
    if boundaries:
        summary_uuid = boundaries[-1].summary_uuid
        for index, message in enumerate(history):
            if message.uuid == summary_uuid:
                working_set = history[index:]
                break
    return SessionSnapshot(
        history=history,
        working_set=working_set,
        content_replacements=snapshot.content_replacements,
        compact_boundaries=boundaries,
        metadata=snapshot.metadata,
    )
