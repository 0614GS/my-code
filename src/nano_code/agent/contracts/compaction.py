"""摘要生成结果和 compact 提交计划。"""

from dataclasses import dataclass

from nano_code.conversation import ConversationSummaryMessage, TokenUsage

from .session import CompactBoundary, ContentReplacement


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """摘要模型成功后、尚未写入 Transcript 的提交计划。"""

    replacements: tuple[ContentReplacement, ...]
    summary: ConversationSummaryMessage
    boundary: CompactBoundary
    usage: TokenUsage


__all__ = ["CompactionOutcome"]
