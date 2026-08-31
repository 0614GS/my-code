"""使用独立模型请求生成可继续工作的会话摘要。"""

import re
from collections.abc import Callable

from my_code.context.models import CompactionOutcome, ContextBudget
from my_code.context.planner import ContextPlanner
from my_code.context.session_cache import ContextPlanningInput
from my_code.conversation.attachments import is_durable_attachment
from my_code.conversation.models import (
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
)
from my_code.conversation.state import CompactBoundary, CompactTrigger
from my_code.model.capabilities import ActiveModelEnvironment
from my_code.model.client import ModelClient, collect_model_output
from my_code.model.errors import ModelContextOverflow
from my_code.model.events import ModelOutputCompleted
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    ModelInvocationCoordinator,
    ModelInvocationRecorder,
    RequestPurpose,
)
from my_code.model.primitives import TokenUsage
from my_code.model.request import (
    InputText,
    ModelInputItem,
    ModelRequest,
    ModelTextBlock,
    SystemPrompt,
    UserInput,
)

_COMPACTION_SYSTEM_PROMPT = """You are a coding-agent conversation compactor.
Your only task is to turn the supplied conversation into accurate continuation
state. Do not call tools, continue the task, expose private reasoning, or invent
facts. Respond with one complete, self-contained Markdown handoff for the
continuing agent."""

_COMPACTION_REQUEST = """Create the continuation summary now.

Inspect the conversation chronologically and preserve:
- every explicit user request, correction, constraint, and change of intent;
- actions taken, files read or changed, important code/API details, and decisions;
- tool and test outcomes, errors, attempted fixes, and unresolved uncertainty;
- the exact current work state and the next action, if one is still required.

Write compact but operational continuation state using these Markdown sections:
1. Current goal and user intent
2. User directives and feedback
3. Technical decisions and invariants
4. Files and code state
5. Verification, errors, and fixes
6. Pending work and immediate next step

Preserve the wording of recent user-authored messages when it defines the current
task or corrects earlier direction. Distinguish completed work from proposed work.
Exclude tool-result bulk, redundant narration, and private reasoning. Do not
acknowledge this instruction. Return only the continuation handoff."""

_TRUNCATION_RETRY_REQUEST = (
    _COMPACTION_REQUEST
    + """

The previous response was cut off by the output token limit. Produce a complete
handoff this time and keep its content at or below 16,000 tokens. Prefer concise
coverage of every operationally important fact over detail that cannot fit."""
)

_ANALYSIS_PATTERN = re.compile(
    r"<(?:analysis|analyze)(?:\s[^>]*)?>[\s\S]*?</(?:analysis|analyze)\s*>",
    re.IGNORECASE,
)
_SUMMARY_TAG_PATTERN = re.compile(
    r"</?summary(?:\s[^>]*)?>?",
    re.IGNORECASE,
)
_DEFAULT_MAX_OUTPUT_TOKENS = 20_000
_MAX_OUTPUT_RETRIES = 1
_MAX_INPUT_RETRIES = 3
_OMITTED_CONTEXT_MARKER = (
    "[Earlier context was omitted because the compact request exceeded the "
    "model input limit.]"
)
_RECENT_USER_MESSAGE_LIMIT = 3
_RECENT_USER_CHAR_LIMIT = 6_000
_CONTINUATION_PREAMBLE = """This session continues from an earlier conversation
that was compacted.
The summary below is prior conversation state, not a new user request. Use it to
continue the current task without acknowledging the compaction or repeating the
summary to the user."""


class ContextCompactor:
    """生成 compact 摘要与无持久化副作用的 context outcome。"""

    def __init__(
        self,
        provider: ModelClient,
        *,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        model_environment: Callable[[], ActiveModelEnvironment] | None = None,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.max_output_tokens = max_output_tokens
        self._model_environment = model_environment

    async def summarize(
        self,
        messages: tuple[ModelInputItem, ...],
        *,
        recorder: ModelInvocationRecorder | None = None,
        causal_head: str | None = None,
        trigger: CompactTrigger | None = None,
    ) -> tuple[str, TokenUsage]:
        output_budget = self._effective_output_budget()
        groups = _conversation_turn_groups(messages)
        visible_groups = groups
        input_retries = 0
        output_attempts = 0
        usage_parts: list[TokenUsage] = []
        omitted_earlier_context = False

        while True:
            visible_messages = _render_visible_groups(
                visible_groups,
                omitted_earlier_context=omitted_earlier_context,
            )
            request_text = (
                _TRUNCATION_RETRY_REQUEST
                if output_attempts > 0
                else _COMPACTION_REQUEST
            )
            request = ModelRequest(
                system_prompt=SystemPrompt.from_text(
                    _COMPACTION_SYSTEM_PROMPT,
                    key="my-code.compaction",
                ),
                input=_append_summary_request(visible_messages, request_text),
                tools=(),
                max_output_tokens=output_budget,
                reasoning_mode="disabled",
                session_cache_identity=(
                    session_id
                    if isinstance(
                        session_id := getattr(recorder, "session_id", None), str
                    )
                    else None
                ),
            )
            try:
                if recorder is None:
                    response = await collect_model_output(self.provider, request)
                else:
                    invocation = ModelInvocation(
                        request=request,
                        origins=tuple(
                            ModelInputOrigin(ModelInputOriginKind.COMPACT_INPUT)
                            for _ in request.input
                        ),
                        purpose=RequestPurpose.COMPACT,
                        causal_head=causal_head,
                        step=1,
                        attempt=input_retries + output_attempts + 1,
                        compact_trigger=trigger,
                    )
                    coordinator = ModelInvocationCoordinator(self.provider, recorder)
                    coordinator.prepare(invocation)
                    response = await _collect_audited_output(coordinator, invocation)
            except ModelContextOverflow as error:
                if input_retries >= _MAX_INPUT_RETRIES:
                    raise ModelContextOverflow(
                        "Compaction input still exceeded the model context window "
                        f"after {input_retries} cropping retries"
                    ) from error
                cropped = _crop_oldest_turns(visible_groups)
                if cropped is None:
                    raise ModelContextOverflow(
                        "Compaction input exceeded the model context window and "
                        "only the latest safe conversation turn remains"
                    ) from error
                visible_groups = cropped
                omitted_earlier_context = True
                input_retries += 1
                continue

            usage_parts.append(response.usage)
            if _is_output_truncation(response.stop_reason):
                output_attempts += 1
                if output_attempts > _MAX_OUTPUT_RETRIES:
                    raise RuntimeError(
                        "Compaction output was truncated after "
                        f"{output_attempts} attempts "
                        f"(stop_reason={response.stop_reason}, "
                        f"max_output_tokens={output_budget})"
                    )
                continue

            response_text = "\n".join(
                block.text
                for block in response.content
                if isinstance(block, ModelTextBlock)
            ).strip()
            if not response_text:
                raise RuntimeError("Compaction model returned no text summary")
            return _extract_summary(response_text), _sum_usage(usage_parts)

    def _effective_output_budget(self) -> int:
        if self._model_environment is None:
            return self.max_output_tokens
        model_limit = self._model_environment().descriptor.limits.max_output_tokens
        return min(self.max_output_tokens, model_limit or self.max_output_tokens)

    async def compact(
        self,
        planner: ContextPlanner,
        state: ContextPlanningInput,
        trigger: CompactTrigger,
        recorder: ModelInvocationRecorder | None = None,
        pre_compact_budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        if not state.context_entries:
            raise ValueError("Cannot compact an empty conversation")

        parent_uuid = next(
            (
                message.uuid
                for message in reversed(state.context_entries)
                if not isinstance(message, AttachmentMessage)
                or is_durable_attachment(message.payload)
            ),
            None,
        )
        if parent_uuid is None:
            raise ValueError("Compaction requires a durable causal parent")
        model_messages, replacements = planner.compaction_view(state)
        summary_text, usage = await self.summarize(
            model_messages,
            recorder=recorder,
            causal_head=parent_uuid,
            trigger=trigger,
        )
        summary = ConversationSummaryMessage(
            content=_build_continuation_context(summary_text, state.context_entries),
            parent_uuid=parent_uuid,
        )
        if pre_compact_budget is None:
            pre_compact_tokens, measurement = planner.measure(state.context_entries)
        else:
            pre_compact_tokens = pre_compact_budget.projected_tokens
            measurement = pre_compact_budget.measurement
        boundary = CompactBoundary(
            parent_uuid=parent_uuid,
            summary_uuid=summary.uuid,
            trigger=trigger,
            pre_compact_tokens=max(1, pre_compact_tokens),
            measurement=measurement,
        )
        return CompactionOutcome(
            replacements=replacements,
            summary=summary,
            boundary=boundary,
            usage=usage,
        )


async def _collect_audited_output(
    coordinator: ModelInvocationCoordinator,
    invocation: ModelInvocation,
):
    output = None
    async for event in coordinator.stream(invocation):
        if isinstance(event.payload, ModelOutputCompleted):
            if output is not None:
                raise RuntimeError(
                    "Model stream emitted more than one completed output"
                )
            output = event.payload.output
    if output is None:
        raise RuntimeError("Model stream ended without a completed output")
    return output


def _append_summary_request(
    items: tuple[ModelInputItem, ...],
    request_text: str = _COMPACTION_REQUEST,
) -> tuple[ModelInputItem, ...]:
    instruction = InputText(request_text)
    if items and isinstance(items[-1], UserInput):
        last = items[-1]
        return items[:-1] + (UserInput(content=last.content + (instruction,)),)
    return items + (UserInput(content=(instruction,)),)


def _extract_summary(response_text: str) -> str:
    """Normalize modern Markdown and permissively unwrap legacy output."""

    summary = _strip_enclosing_fence(response_text.strip())
    summary = _ANALYSIS_PATTERN.sub("", summary)
    summary = _SUMMARY_TAG_PATTERN.sub("\n", summary)
    summary = _strip_enclosing_fence(summary.strip())
    summary = summary.strip()
    if not summary:
        raise RuntimeError("Compaction model returned an empty summary")
    return summary


def _is_output_truncation(stop_reason: str) -> bool:
    return stop_reason.casefold() in {"max_tokens", "max_output_tokens"}


def _sum_usage(parts: list[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(part.input_tokens for part in parts),
        output_tokens=sum(part.output_tokens for part in parts),
        cache_creation_input_tokens=sum(
            part.cache_creation_input_tokens for part in parts
        ),
        cache_read_input_tokens=sum(part.cache_read_input_tokens for part in parts),
        provider_reported=all(part.provider_reported for part in parts),
    )


def _conversation_turn_groups(
    items: tuple[ModelInputItem, ...],
) -> tuple[tuple[ModelInputItem, ...], ...]:
    groups: list[tuple[ModelInputItem, ...]] = []
    current: list[ModelInputItem] = []
    seen_user = False
    for item in items:
        if isinstance(item, UserInput) and seen_user:
            groups.append(tuple(current))
            current = []
        current.append(item)
        if isinstance(item, UserInput):
            seen_user = True
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _crop_oldest_turns(
    groups: tuple[tuple[ModelInputItem, ...], ...],
) -> tuple[tuple[ModelInputItem, ...], ...] | None:
    if len(groups) <= 1:
        return None
    remove_count = max(1, len(groups) // 5)
    remove_count = min(remove_count, len(groups) - 1)
    return groups[remove_count:]


def _render_visible_groups(
    groups: tuple[tuple[ModelInputItem, ...], ...],
    *,
    omitted_earlier_context: bool,
) -> tuple[ModelInputItem, ...]:
    flattened = tuple(item for group in groups for item in group)
    if not omitted_earlier_context:
        return flattened
    marker = UserInput((InputText(_OMITTED_CONTEXT_MARKER),))
    return (marker, *flattened)


def _strip_enclosing_fence(text: str) -> str:
    lines = text.splitlines()
    if (
        len(lines) >= 2
        and lines[0].strip().startswith("```")
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text


def _build_continuation_context(
    summary: str,
    messages: tuple[ConversationEntry, ...],
) -> str:
    """构造后续 Agent 看到的 compact 语义信封和用户原文事实。"""

    remaining = _RECENT_USER_CHAR_LIMIT
    newest_first: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        if len(newest_first) >= _RECENT_USER_MESSAGE_LIMIT or remaining == 0:
            break
        content = message.content
        if len(content) <= remaining:
            newest_first.append(content)
            remaining -= len(content)
            continue
        if not newest_first:
            marker = "[earlier portion omitted]\n"
            available = max(0, remaining - len(marker))
            newest_first.append(marker + content[-available:])
        break

    compacted = (
        f"{_CONTINUATION_PREAMBLE}\n\n## Compacted conversation summary\n\n{summary}"
    )
    if not newest_first:
        return compacted
    excerpts = tuple(reversed(newest_first))
    rendered = "\n\n".join(
        f"### User message {index}\n{content}"
        for index, content in enumerate(excerpts, start=1)
    )
    return f"{compacted}\n\n## Recent user messages (verbatim excerpts)\n\n{rendered}"


__all__ = [
    "ContextCompactor",
]
