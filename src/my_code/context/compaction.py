"""使用独立模型请求生成可继续工作的会话摘要。"""

import re

from my_code.context.models import CompactionOutcome
from my_code.context.planner import ContextPlanner
from my_code.context.session import ContextPlanningState
from my_code.conversation.attachments import is_durable_attachment
from my_code.conversation.models import (
    AttachmentMessage,
    ConversationEntry,
    ConversationSummaryMessage,
    HumanMessage,
)
from my_code.conversation.state import CompactBoundary, CompactTrigger
from my_code.model.client import ModelClient, collect_model_output
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
state. Do not call tools, continue the task, or invent facts. Respond with plain
text containing exactly one non-empty <summary> block. Do not add analysis,
Markdown fences, or text outside that block. Only the contents of <summary> will
be shown to the continuing agent."""

_COMPACTION_REQUEST = """Create the continuation summary now.

Inspect the conversation chronologically and preserve:
- every explicit user request, correction, constraint, and change of intent;
- actions taken, files read or changed, important code/API details, and decisions;
- tool and test outcomes, errors, attempted fixes, and unresolved uncertainty;
- the exact current work state and the next action, if one is still required.

In <summary>, write compact but operational continuation state using these sections:
1. Current goal and user intent
2. User directives and feedback
3. Technical decisions and invariants
4. Files and code state
5. Verification, errors, and fixes
6. Pending work and immediate next step

Preserve the wording of recent user-authored messages when it defines the current
task or corrects earlier direction. Distinguish completed work from proposed work.
Exclude tool-result bulk, redundant narration, and private reasoning. Do not
acknowledge this instruction or add text outside the summary XML tag.

Required response shape:
<summary>
continuation state
</summary>"""

_SUMMARY_PATTERN = re.compile(r"<summary>([\s\S]*?)</summary>")
_ANALYZE_PATTERN = re.compile(r"<analyze>[\s\S]*?</analyze>")
_RECENT_USER_MESSAGE_LIMIT = 3
_RECENT_USER_CHAR_LIMIT = 6_000
_CONTINUATION_PREAMBLE = """This session continues from an earlier conversation
that was compacted.
The summary below is prior conversation state, not a new user request. Use it to
continue the current task without acknowledging the compaction or repeating the
summary to the user."""


class ContextCompactor:
    """生成 compact 摘要与无持久化副作用的 context outcome。"""

    def __init__(self, provider: ModelClient, *, max_output_tokens: int = 2048) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.max_output_tokens = max_output_tokens

    async def summarize(
        self, messages: tuple[ModelInputItem, ...]
    ) -> tuple[str, TokenUsage]:
        response = await collect_model_output(
            self.provider,
            ModelRequest(
                system_prompt=SystemPrompt.from_text(
                    _COMPACTION_SYSTEM_PROMPT,
                    key="my-code.compaction",
                ),
                input=_append_summary_request(messages),
                tools=(),
                max_output_tokens=self.max_output_tokens,
            ),
        )
        response_text = "\n".join(
            block.text
            for block in response.content
            if isinstance(block, ModelTextBlock)
        ).strip()
        if not response_text:
            raise RuntimeError("Compaction model returned no text summary")
        summary = _extract_summary(response_text)
        return summary, response.usage

    async def compact(
        self,
        planner: ContextPlanner,
        state: ContextPlanningState,
        trigger: CompactTrigger,
    ) -> CompactionOutcome:
        if not state.context_entries:
            raise ValueError("Cannot compact an empty conversation")

        model_messages, replacements = planner.compaction_view(state)
        summary_text, usage = await self.summarize(model_messages)
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
        summary = ConversationSummaryMessage(
            content=_build_continuation_context(summary_text, state.context_entries),
            parent_uuid=parent_uuid,
        )
        boundary = CompactBoundary(
            parent_uuid=parent_uuid,
            summary_uuid=summary.uuid,
            trigger=trigger,
            pre_compact_chars=max(1, planner.measure(state.context_entries)),
        )
        return CompactionOutcome(
            replacements=replacements,
            summary=summary,
            boundary=boundary,
            usage=usage,
        )


def _append_summary_request(
    items: tuple[ModelInputItem, ...],
) -> tuple[ModelInputItem, ...]:
    instruction = InputText(_COMPACTION_REQUEST)
    if items and isinstance(items[-1], UserInput):
        last = items[-1]
        return items[:-1] + (UserInput(content=last.content + (instruction,)),)
    return items + (UserInput(content=(instruction,)),)


def _extract_summary(response_text: str) -> str:
    """Extract a tagged summary or one unambiguously plain fallback body."""

    matches: list[str] = _SUMMARY_PATTERN.findall(response_text)
    opening_markers = response_text.count("<summary")
    closing_markers = response_text.count("</summary")
    if (
        len(matches) == 1
        and opening_markers == 1
        and closing_markers == 1
        and matches[0].strip()
    ):
        return matches[0].strip()
    if len(matches) > 1 or "<summary" in response_text or "</summary" in response_text:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    fallback = _strip_enclosing_fence(response_text.strip())
    analyze_matches = _ANALYZE_PATTERN.findall(fallback)
    if len(analyze_matches) > 1:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    if analyze_matches:
        fallback = _ANALYZE_PATTERN.sub("", fallback, count=1)
    if "<analyze" in fallback or "</analyze" in fallback:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    summary = fallback.strip()
    if not summary:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    return summary


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
