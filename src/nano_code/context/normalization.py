"""将 Transcript 和注入上下文规范化为模型输入消息。"""

from dataclasses import replace

from nano_code.agent.contracts.context import (
    EphemeralContextContentBlock,
    EphemeralContextMessage,
)
from nano_code.agent.contracts.model import ModelInputContentBlock, ModelInputMessage
from nano_code.messages import (
    ContentBlock,
    SystemContextBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)
from nano_code.messages.xml import render_system_context


class ModelInputNormalizer:
    """生成模型输入、移除本地信息、合并消息并验证工具协议。"""

    def normalize_transcript(
        self, messages: tuple[TranscriptMessage, ...]
    ) -> tuple[ModelInputMessage, ...]:
        normalized: list[ModelInputMessage] = []
        for message in messages:
            # 展示快照属于 Transcript/UI，不进入模型可见的输入。
            content = tuple(
                _normalize_transcript_block(block) for block in message.content
            )
            candidate = ModelInputMessage(role=message.role, content=content)
            if normalized and normalized[-1].role == candidate.role:
                previous = normalized[-1]
                normalized[-1] = ModelInputMessage(
                    role=previous.role,
                    content=previous.content + candidate.content,
                )
            else:
                normalized.append(candidate)

        result = tuple(normalized)
        self._validate_tool_pairs(result)
        return result

    def normalize_context(
        self, messages: tuple[EphemeralContextMessage, ...]
    ) -> tuple[ModelInputMessage, ...]:
        """Normalize non-history context without adding it to the Transcript."""

        normalized: list[ModelInputMessage] = []
        for message in messages:
            candidate = ModelInputMessage(
                role=message.role,
                content=tuple(
                    _normalize_context_block(block) for block in message.content
                ),
            )
            if normalized and normalized[-1].role == candidate.role:
                previous = normalized[-1]
                normalized[-1] = ModelInputMessage(
                    role=previous.role,
                    content=previous.content + candidate.content,
                )
            else:
                normalized.append(candidate)
        return tuple(normalized)

    @staticmethod
    def _validate_tool_pairs(messages: tuple[ModelInputMessage, ...]) -> None:
        pending: set[str] = set()
        seen_calls: set[str] = set()
        seen_results: set[str] = set()
        for message in messages:
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    if block.id in seen_calls:
                        raise ValueError(f"Duplicate tool use in context: {block.id}")
                    seen_calls.add(block.id)
                    pending.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id in seen_results:
                        raise ValueError(
                            f"Duplicate tool result in context: {block.tool_use_id}"
                        )
                    if block.tool_use_id not in pending:
                        raise ValueError(
                            f"Orphan tool result in context: {block.tool_use_id}"
                        )
                    seen_results.add(block.tool_use_id)
                    pending.remove(block.tool_use_id)
        if pending:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"Unresolved tool use in context: {unresolved}")


def _normalize_transcript_block(block: ContentBlock) -> ModelInputContentBlock:
    """在唯一边界移除本地展示数据并渲染可信上下文。"""

    if isinstance(block, SystemContextBlock):
        return TextBlock(render_system_context(block))
    if isinstance(block, ToolResultBlock):
        return replace(block, presentation=None)
    return block


def _normalize_context_block(block: EphemeralContextContentBlock) -> TextBlock:
    if isinstance(block, SystemContextBlock):
        return TextBlock(render_system_context(block))
    return block
