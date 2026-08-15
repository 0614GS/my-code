"""使用独立模型请求生成可继续工作的会话摘要。"""

from dataclasses import dataclass

from nano_code.context.models import ContextPlan, ModelMessage
from nano_code.messages import TextBlock, TokenUsage
from nano_code.prompts import SystemPrompt
from nano_code.providers.base import ModelProvider

_COMPACTION_SYSTEM_PROMPT = """You compact coding-agent conversations.
Create a concise continuation summary that preserves the user's goal, important
decisions, files inspected or changed, tool outcomes, unresolved errors, and the
next concrete steps. Do not invent facts. Return only the summary."""
_COMPACTION_REQUEST = "Produce the continuation summary now."


@dataclass(frozen=True, slots=True)
class CompactionResult:
    summary: str
    usage: TokenUsage


class CompactionService:
    """与主 Agent Loop 分离的摘要模型调用。"""

    def __init__(
        self, provider: ModelProvider, *, max_output_tokens: int = 2048
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
