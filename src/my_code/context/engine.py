"""Context 模块对外的规划、检查与压缩能力。"""

import hashlib
from dataclasses import replace

from my_code.context.compaction import ContextCompactor
from my_code.context.models import CompactionOutcome, ContextBudget, ContextPlan
from my_code.context.planner import ContextPlanner
from my_code.context.rebuild import PostCompactContextRebuilder
from my_code.context.recent_files import PostCompactFileRecovery
from my_code.context.session_cache import (
    AttachmentProjectionInput,
    CompactionInput,
    ContextPlanningInput,
    SessionContextCache,
)
from my_code.conversation.attachments import (
    AttachmentPayload,
    RecentFileSnapshotAttachment,
)
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
        rebuilder: PostCompactContextRebuilder | None = None,
        file_recovery: PostCompactFileRecovery | None = None,
    ) -> None:
        self._planner = planner
        self._compactor = compactor
        self._rebuilder = rebuilder or PostCompactContextRebuilder()
        self._file_recovery = file_recovery

    def plan(
        self,
        state: ContextPlanningInput,
        runtime: SessionContextCache,
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
        state: ContextPlanningInput,
        runtime: SessionContextCache,
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
        self, state: AttachmentProjectionInput
    ) -> tuple[AttachmentPayload, ...]:
        return self._planner.derive_attachments(state)

    async def compact(
        self,
        state: CompactionInput,
        trigger: CompactTrigger,
        recorder: ModelInvocationRecorder | None = None,
        pre_compact_budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        """摘要成功后恢复关键状态；任一阶段失败都不返回可提交提案。"""
        outcome = await self._compactor.compact(
            self._planner,
            state.planning,
            trigger,
            recorder=recorder,
            pre_compact_budget=pre_compact_budget,
        )
        attachments = self._rebuilder.rebuild(state)
        if self._file_recovery is None:
            return replace(outcome, attachments=attachments)
        summary_sha256 = hashlib.sha256(outcome.summary.content.encode()).hexdigest()
        prepared = await self._file_recovery.prepare(state.session_id, summary_sha256)
        try:
            recent = tuple(
                RecentFileSnapshotAttachment(
                    item.path,
                    item.text,
                    item.sha256,
                    item.total_lines,
                    item.start_line,
                    item.end_line,
                    item.truncated,
                )
                for item in prepared.files
            )
        except BaseException:
            self._file_recovery.discard(prepared.receipt)
            raise
        return replace(
            outcome,
            attachments=(*attachments, *recent),
            recovery_receipt=prepared.receipt,
        )

    async def acknowledge_compaction(self, outcome: CompactionOutcome) -> None:
        receipt = outcome.recovery_receipt
        if receipt is not None and self._file_recovery is not None:
            await self._file_recovery.acknowledge(receipt)

    def discard_compaction(self, outcome: CompactionOutcome) -> None:
        receipt = outcome.recovery_receipt
        if receipt is not None and self._file_recovery is not None:
            self._file_recovery.discard(receipt)


__all__ = ["ContextEngine"]
