"""Safe resumed-session history projection."""

from my_code.application.contracts.history import (
    HistoryContextGroup,
    HistoryContextItem,
    HistoryEntry,
    HistoryPlan,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
)
from my_code.conversation.models import (
    AssistantMessage,
    ConversationSummaryMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.proposed_plan import (
    extract_proposed_plan,
    strip_proposed_plan,
)
from my_code.features.todos.codec import TODO_WRITE_TOOL_NAME, parse_todo_input
from my_code.model.invocation import ModelInputOriginKind
from my_code.model.tool_search import ToolSearchMode
from my_code.sessions.session import Session
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import (
    ToolExposureSnapshot,
    restored_discoveries,
    unwrap_searched_tool_call,
)
from my_code.tools.executor import ToolExecutor


def project_history(
    session: Session,
    *,
    catalog: ToolCatalogSnapshot,
    search_mode: ToolSearchMode,
    tool_executor: ToolExecutor,
) -> tuple[HistoryEntry, ...]:
    tools = ToolExposureSnapshot.build(
        catalog, search_mode, restored_discoveries(session.conversation)
    )
    results = {
        block.tool_use_id: block
        for message in session.conversation
        if isinstance(message, ToolResultBatch)
        for block in message.content
        if isinstance(block, ToolResult)
    }
    history: list[HistoryEntry] = []
    context_groups = _history_context_groups(session)
    history.extend(context_groups.pop(None, ()))
    for message in session.conversation:
        if isinstance(message, HumanMessage):
            history.append(HistoryText("user", message.content))
        elif isinstance(message, ConversationSummaryMessage):
            history.append(HistoryText("system", "Conversation compacted"))
        elif isinstance(message, AssistantMessage):
            tool_ids = [
                block.id for block in message.content if isinstance(block, ToolCall)
            ]
            for block in message.content:
                if isinstance(block, TextContent) and block.text:
                    visible = strip_proposed_plan(block.text)
                    if visible.strip():
                        history.append(
                            HistoryText(
                                "assistant", visible, is_final_answer=not tool_ids
                            )
                        )
                    plan = extract_proposed_plan(block.text)
                    if plan:
                        history.append(HistoryPlan(plan))
                elif isinstance(block, ReasoningContent):
                    history.append(HistoryReasoning(block.presentation))
                elif isinstance(block, ToolCall):
                    result = results.get(block.id)
                    todos = None
                    semantic_call = unwrap_searched_tool_call(block)
                    if (
                        semantic_call.name == TODO_WRITE_TOOL_NAME
                        and result is not None
                        and not result.is_error
                    ):
                        try:
                            todos = parse_todo_input(semantic_call.input)
                        except (TypeError, ValueError):
                            pass
                    history.append(
                        HistoryToolCall(
                            tool_use_id=block.id,
                            use=tool_executor.present_use(block, tools=tools),
                            result=(
                                result.presentation
                                if result is not None
                                else tool_executor.present_error(
                                    block,
                                    "Tool result is missing from the transcript.",
                                    tools=tools,
                                )
                            ),
                            is_error=result is None or result.is_error,
                            todos=todos,
                            ends_tool_batch=bool(tool_ids) and block.id == tool_ids[-1],
                            name=block.name,
                            input=block.input,
                        )
                    )
        history.extend(context_groups.pop(message.uuid, ()))
    for unmatched in context_groups.values():
        history.extend(unmatched)
    return tuple(history)


def _history_context_groups(
    session: Session,
) -> dict[str | None, tuple[HistoryContextGroup, ...]]:
    grouped: dict[str | None, list[HistoryContextGroup]] = {}
    previous_refs: set[str] = set()
    visible_origins = {
        ModelInputOriginKind.USER_CONTEXT,
        ModelInputOriginKind.ATTACHMENT,
        ModelInputOriginKind.CONTENT_REPLACEMENT,
    }
    for request in session.request_audit_snapshot().requests:
        manifest = request.manifest
        items: list[HistoryContextItem] = []
        if manifest.purpose.value != "compact":
            for value, origin, audit_id in zip(
                request.input,
                manifest.origins,
                manifest.input_refs,
                strict=True,
            ):
                if audit_id in previous_refs or origin.kind not in visible_origins:
                    continue
                items.append(
                    HistoryContextItem(
                        origin.source or origin.kind.value,
                        origin.attachment_kind,
                        _audit_input_text(value),
                    )
                )
        previous_refs.update(manifest.input_refs)
        if items:
            grouped.setdefault(manifest.causal_head, []).append(
                HistoryContextGroup(manifest.request_number, tuple(items))
            )
    return {key: tuple(value) for key, value in grouped.items()}


def _audit_input_text(value: object) -> str:
    if not isinstance(value, dict) or value.get("type") != "user_input":
        return ""
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        text
        for block in content
        if isinstance(block, dict) and block.get("type") == "input_text"
        if isinstance((text := block.get("text")), str)
    )


__all__ = ["project_history"]
