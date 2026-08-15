"""摘要生成结果和 compact 提交计划。"""

from dataclasses import dataclass

from nano_code.messages import ChatMessage, SystemContextBlock, TokenUsage

from .session import CompactBoundary, ContentReplacement


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """摘要模型成功后、尚未写入 Transcript 的提交计划。"""

    replacements: tuple[ContentReplacement, ...]
    summary: ChatMessage
    boundary: CompactBoundary
    usage: TokenUsage

    @property
    def summary_message(self) -> ChatMessage:
        return self.summary

    @property
    def content_replacements(self) -> tuple[ContentReplacement, ...]:
        return self.replacements

    @property
    def summary_text(self) -> str:
        block = self.summary.content[0]
        if isinstance(block, SystemContextBlock):
            return block.content
        raise TypeError("Compaction summary must contain a system context block")


__all__ = ["CompactionOutcome"]
