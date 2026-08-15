"""compact 生成使用的 outbound port。"""

from typing import Protocol, runtime_checkable

from nano_code.agent.contracts.compaction import CompactionOutcome
from nano_code.agent.contracts.session import CompactTrigger, ConversationSnapshot


@runtime_checkable
class CompactorPort(Protocol):
    """生成尚未持久化的 compact 提交计划。"""

    async def compact(
        self,
        snapshot: ConversationSnapshot,
        trigger: CompactTrigger,
    ) -> CompactionOutcome: ...


__all__ = ["CompactorPort"]
