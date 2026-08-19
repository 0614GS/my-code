"""使用独立模型请求生成可继续工作的会话摘要。"""

import re
from dataclasses import dataclass
from typing import Protocol

from nano_code.context.models import CompactionOutcome
from nano_code.context.planner import ContextBuilder
from nano_code.context.session import ContextSnapshot
from nano_code.conversation.models import (
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
)
from nano_code.conversation.state import CompactBoundary, CompactTrigger
from nano_code.model.client import ModelClient, collect_model_output
from nano_code.model.primitives import TokenUsage
from nano_code.model.request import (
    ModelMessage,
    ModelRequest,
    ModelTextBlock,
    ModelUserMessage,
    SystemPrompt,
)

_COMPACTION_SYSTEM_PROMPT = """You are a coding-agent conversation compactor.
Your only task is to turn the supplied conversation into accurate continuation
state. Do not call tools, continue the task, or invent facts. Respond with plain
text containing exactly one <analyze> block followed by exactly one <summary>
block. The <analyze> block is a private completeness check and will be discarded.
Only the contents of <summary> will be shown to the continuing agent."""

_COMPACTION_REQUEST = """Create the continuation summary now.

In <analyze>, inspect the conversation chronologically and verify that you found:
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
Exclude tool-result bulk, redundant narration, and the discarded analysis. Do not
acknowledge this instruction or add text outside the two XML tags.

Required response shape:
<analyze>
completeness check
</analyze>
<summary>
continuation state
</summary>"""

_SUMMARY_PATTERN = re.compile(r"<summary>([\s\S]*?)</summary>")
_RECENT_USER_MESSAGE_LIMIT = 3
_RECENT_USER_CHAR_LIMIT = 6_000
_CONTINUATION_PREAMBLE = """This session continues from an earlier conversation
that was compacted.
The summary below is prior conversation state, not a new user request. Use it to
continue the current task without acknowledging the compaction or repeating the
summary to the user."""


@dataclass(frozen=True, slots=True)
class CompactionResult:
    summary: str
    usage: TokenUsage


class _CompactionSummarizer(Protocol):
    """CompactionCoordinator 的 adapter 内部依赖。"""

    async def summarize(
        self, messages: tuple[ModelMessage, ...]
    ) -> CompactionResult: ...


class CompactionService:
    """与主 Agent Loop 分离的摘要模型调用。"""

    def __init__(self, provider: ModelClient, *, max_output_tokens: int = 2048) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.max_output_tokens = max_output_tokens

    async def summarize(self, messages: tuple[ModelMessage, ...]) -> CompactionResult:
        response = await collect_model_output(
            self.provider,
            ModelRequest(
                system_prompt=SystemPrompt.from_text(
                    _COMPACTION_SYSTEM_PROMPT,
                    key="nano-code.compaction",
                ),
                messages=_append_summary_request(messages),
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
        return CompactionResult(summary=summary, usage=response.usage)


class CompactionCoordinator:
    """Coordinate Context projection with the dedicated summary model call.

    摘要和边界都在这里构造，但直到调用方把返回的 outcome 交给
    ``Session.commit_compaction`` 前，不会产生任何持久化副作用。
    """

    def __init__(self, context: ContextBuilder, service: _CompactionSummarizer) -> None:
        self.context = context
        self.service = service

    async def compact(
        self,
        snapshot: ContextSnapshot,
        trigger: CompactTrigger,
    ) -> CompactionOutcome:
        if not snapshot.messages:
            raise ValueError("Cannot compact an empty conversation")

        model_messages, replacements = self.context.compaction_view(snapshot)
        result = await self.service.summarize(model_messages)
        parent_uuid = snapshot.messages[-1].uuid
        summary = ConversationSummaryMessage(
            content=_build_continuation_context(result.summary, snapshot.messages),
            parent_uuid=parent_uuid,
        )
        boundary = CompactBoundary(
            parent_uuid=parent_uuid,
            summary_uuid=summary.uuid,
            trigger=trigger,
            pre_compact_chars=max(1, self.context.measure(snapshot.messages)),
        )
        return CompactionOutcome(
            replacements=replacements,
            summary=summary,
            boundary=boundary,
            usage=result.usage,
        )


def _append_summary_request(
    messages: tuple[ModelMessage, ...],
) -> tuple[ModelMessage, ...]:
    instruction = ModelTextBlock(_COMPACTION_REQUEST)
    if messages and messages[-1].role == "user":
        last = messages[-1]
        return messages[:-1] + (
            ModelUserMessage(content=last.content + (instruction,)),
        )
    return messages + (ModelUserMessage(content=(instruction,)),)


def _extract_summary(response_text: str) -> str:
    """提取唯一 summary 节点，确保 analyze 草稿不会进入后续上下文。"""

    matches: list[str] = _SUMMARY_PATTERN.findall(response_text)
    if len(matches) != 1:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    summary = matches[0].strip()
    if not summary:
        raise RuntimeError(
            "Compaction model must return exactly one non-empty <summary> block"
        )
    return summary


def _build_continuation_context(
    summary: str,
    messages: tuple[ConversationMessage, ...],
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
    "CompactionCoordinator",
    "CompactionResult",
    "CompactionService",
]
