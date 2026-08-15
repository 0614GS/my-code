"""使用独立模型请求生成可继续工作的会话摘要。"""

from dataclasses import dataclass
from typing import Protocol

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.context import ContextPlan
from nano_code.agent.contracts.model import ModelMessage
from nano_code.agent.contracts.session import (
    CompactBoundary,
    CompactTrigger,
    ConversationSnapshot,
)
from nano_code.agent.ports.compaction import CompactorPort
from nano_code.agent.ports.context import ContextPort
from nano_code.agent.ports.model import ModelCompletionPort
from nano_code.messages import ChatMessage, SystemContextBlock, TextBlock, TokenUsage
from nano_code.prompts import SystemPrompt

_COMPACTION_SYSTEM_PROMPT = """You compact coding-agent conversations.
Create a concise continuation summary that preserves the user's goal, important
decisions, files inspected or changed, tool outcomes, unresolved errors, and the
next concrete steps. Do not invent facts. Return only the summary."""
_COMPACTION_REQUEST = "Produce the continuation summary now."


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

    def __init__(
        self, provider: ModelCompletionPort, *, max_output_tokens: int = 2048
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.max_output_tokens = max_output_tokens

    async def summarize(self, messages: tuple[ModelMessage, ...]) -> CompactionResult:
        response = await self.provider.complete(
            ContextPlan(
                system_prompt=SystemPrompt.from_text(
                    _COMPACTION_SYSTEM_PROMPT,
                    key="nano-code.compaction",
                ),
                messages=_append_summary_request(messages),
                tools=(),
                max_output_tokens=self.max_output_tokens,
            )
        )
        summary = "\n".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not summary:
            raise RuntimeError("Compaction model returned no text summary")
        return CompactionResult(summary=summary, usage=response.usage)


class CompactionCoordinator(CompactorPort):
    """连接 ContextPort 与摘要服务的纯编排适配器。

    摘要和边界都在这里构造，但直到调用方把返回的 outcome 交给
    ``ConversationState.commit_compaction`` 前，不会产生任何持久化副作用。
    """

    def __init__(self, context: ContextPort, service: _CompactionSummarizer) -> None:
        self.context = context
        self.service = service

    async def compact(
        self,
        snapshot: ConversationSnapshot,
        trigger: CompactTrigger,
    ) -> CompactionOutcome:
        if not snapshot.messages:
            raise ValueError("Cannot compact an empty conversation")

        model_messages, replacements = self.context.compaction_view(snapshot)
        result = await self.service.summarize(model_messages)
        parent_uuid = snapshot.messages[-1].uuid
        summary = ChatMessage(
            role="user",
            origin="system",
            content=(
                SystemContextBlock(
                    kind="conversation_summary",
                    content=result.summary,
                ),
            ),
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
    instruction = TextBlock(_COMPACTION_REQUEST)
    if messages and messages[-1].role == "user":
        last = messages[-1]
        return messages[:-1] + (
            ModelMessage(role="user", content=last.content + (instruction,)),
        )
    return messages + (ModelMessage(role="user", content=(instruction,)),)
