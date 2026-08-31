"""Age-gated token microcompaction of replayable tool results."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from my_code.conversation.models import (
    AssistantMessage,
    ConversationEntry,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import ContentReplacement

_ELIGIBLE_TOOLS = frozenset({"Glob", "Grep", "Read"})


class MicrocompactPolicy:
    """Make one oldest-first pass immediately before full compaction."""

    def __init__(
        self,
        *,
        minimum_age: timedelta = timedelta(minutes=30),
        keep_recent_results: int = 5,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if minimum_age < timedelta(0) or keep_recent_results < 0:
            raise ValueError("Microcompact limits are invalid")
        self.minimum_age = minimum_age
        self.keep_recent_results = keep_recent_results
        self._now = now or (lambda: datetime.now(UTC))

    def propose(
        self,
        messages: tuple[ConversationEntry, ...],
        existing: tuple[ContentReplacement, ...],
        *,
        current_tokens: int,
        trigger_tokens: int,
        estimate: Callable[[tuple[ConversationEntry, ...]], int],
    ) -> tuple[ContentReplacement, ...]:
        if current_tokens < trigger_tokens:
            return ()
        replacements = {item.tool_use_id: item for item in existing}
        tool_names = {
            block.id: block.name
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCall)
        }
        candidates = self._candidates(messages, replacements, tool_names)
        proposed: list[ContentReplacement] = []
        effective_tokens = current_tokens
        for result in candidates:
            replacement = ContentReplacement.for_tool_result(
                tool_use_id=result.tool_use_id,
                tool_name=tool_names[result.tool_use_id],
                original_chars=len(result.content),
            )
            trial = tuple(proposed) + (replacement,)
            view = apply_content_replacements(messages, existing + trial)
            trial_tokens = estimate(view)
            if trial_tokens >= effective_tokens:
                continue
            proposed.append(replacement)
            effective_tokens = trial_tokens
            if effective_tokens < trigger_tokens:
                break
        return tuple(proposed)

    def _candidates(
        self,
        messages: tuple[ConversationEntry, ...],
        replacements: dict[str, ContentReplacement],
        tool_names: dict[str, str],
    ) -> list[ToolResult]:
        cutoff = self._now() - self.minimum_age
        replayable = [
            (batch, result)
            for batch in messages
            if isinstance(batch, ToolResultBatch)
            for result in batch.content
            if not result.is_error
            and result.tool_use_id not in replacements
            and tool_names.get(result.tool_use_id) in _ELIGIBLE_TOOLS
        ]
        protected = {
            result.tool_use_id for _, result in replayable[-self.keep_recent_results :]
        }
        return [
            result
            for batch, result in replayable
            if result.tool_use_id not in protected
            and _timestamp(batch.timestamp) <= cutoff
        ]


def apply_content_replacements(
    messages: tuple[ConversationEntry, ...],
    replacements: tuple[ContentReplacement, ...],
) -> tuple[ConversationEntry, ...]:
    by_id = {item.tool_use_id: item for item in replacements}
    return tuple(
        replace(
            message,
            content=tuple(
                replace(result, content=by_id[result.tool_use_id].content)
                if result.tool_use_id in by_id
                else result
                for result in message.content
            ),
        )
        if isinstance(message, ToolResultBatch)
        else message
        for message in messages
    )


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["MicrocompactPolicy", "apply_content_replacements"]
