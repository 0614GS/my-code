"""Context 模块对外的规划、检查与压缩能力。"""

from my_code.context.compaction import ContextCompactor
from my_code.context.models import CompactionOutcome, ContextBudget, ContextPlan
from my_code.context.planner import ContextPlanner
from my_code.context.session import (
    AttachmentDelivery,
    ContextSnapshot,
    SessionContextAccess,
)
from my_code.conversation.state import CompactTrigger
from my_code.model.request import ModelToolDefinition, ResolvedPromptSection


class ContextEngine:
    """组合无状态规划器与摘要模型调用，不直接提交会话事实。"""

    def __init__(
        self,
        planner: ContextPlanner,
        compactor: ContextCompactor,
    ) -> None:
        self._planner = planner
        self._compactor = compactor

    def plan(
        self,
        snapshot: ContextSnapshot,
        session: SessionContextAccess | None = None,
        *,
        tools: tuple[ModelToolDefinition, ...],
        prompt_sections: tuple[ResolvedPromptSection, ...] = (),
    ) -> ContextPlan:
        return self._planner.plan(
            snapshot,
            session,
            tools=tools,
            prompt_sections=prompt_sections,
        )

    def inspect(
        self,
        snapshot: ContextSnapshot,
        session: SessionContextAccess | None = None,
        *,
        tools: tuple[ModelToolDefinition, ...],
        prompt_sections: tuple[ResolvedPromptSection, ...] = (),
    ) -> ContextBudget:
        return self._planner.inspect(
            snapshot,
            session,
            tools=tools,
            prompt_sections=prompt_sections,
        )

    def acknowledge_attachments(
        self,
        deliveries: tuple[AttachmentDelivery, ...],
    ) -> None:
        self._planner.acknowledge_attachments(deliveries)

    async def compact(
        self,
        snapshot: ContextSnapshot,
        trigger: CompactTrigger,
    ) -> CompactionOutcome:
        return await self._compactor.compact(self._planner, snapshot, trigger)


__all__ = ["ContextEngine"]
