"""Context 模块对外的规划、检查与压缩能力。"""

from my_code.context.compaction import ContextCompactor
from my_code.context.models import CompactionOutcome, ContextBudget, ContextPlan
from my_code.context.planner import ContextPlanner
from my_code.context.session import (
    AttachmentDerivationState,
    ContextPlanningState,
    ContextRuntime,
)
from my_code.conversation.attachments import AttachmentPayload
from my_code.conversation.state import CompactTrigger
from my_code.model.invocation import ModelInvocationRecorder
from my_code.model.primitives import ContextFootprint, TokenUsage
from my_code.model.request import AssistantOutput, ModelToolDefinition


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
        state: ContextPlanningState,
        runtime: ContextRuntime,
        *,
        tools: tuple[ModelToolDefinition, ...],
    ) -> ContextPlan:
        return self._planner.plan(
            state,
            runtime,
            tools=tools,
        )

    def inspect(
        self,
        state: ContextPlanningState,
        runtime: ContextRuntime,
        *,
        tools: tuple[ModelToolDefinition, ...],
    ) -> ContextBudget:
        return self._planner.inspect(
            state,
            runtime,
            tools=tools,
        )

    def acknowledge_attachments(
        self,
        attachments: tuple[AttachmentPayload, ...],
    ) -> None:
        self._planner.acknowledge_attachments(attachments)

    def record_response(
        self, plan: ContextPlan, response: AssistantOutput, usage: TokenUsage
    ) -> ContextFootprint:
        return self._planner.record_response(plan, response, usage)

    def derive_attachments(
        self, state: AttachmentDerivationState
    ) -> tuple[AttachmentPayload, ...]:
        return self._planner.derive_attachments(state)

    async def compact(
        self,
        state: ContextPlanningState,
        trigger: CompactTrigger,
        recorder: ModelInvocationRecorder | None = None,
        pre_compact_budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        return await self._compactor.compact(
            self._planner,
            state,
            trigger,
            recorder=recorder,
            pre_compact_budget=pre_compact_budget,
        )


__all__ = ["ContextEngine"]
