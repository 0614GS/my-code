"""一次 ToolRound 的事件与串行执行。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from my_code.conversation.attachments import (
    AttachmentPayload,
    ToolDiscoveryAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.permissions.models import PermissionMode, PermissionUpdate
from my_code.permissions.policy import PermissionPolicy
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import ToolExposureSnapshot
from my_code.tools.executor import ToolExecutionOutcome
from my_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call: ToolCall
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    call: ToolCall
    result: ToolResult
    presentation: ToolResultPresentation
    new_attachments: tuple[AttachmentPayload, ...] = ()
    permission_updates: tuple[PermissionUpdate, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolRoundCompleted:
    message: ToolResultBatch
    new_attachments: tuple[AttachmentPayload, ...] = ()
    permission_updates: tuple[PermissionUpdate, ...] = ()
    cancelled: bool = False


type ToolRoundEvent = ToolCallStarted | ToolCallFinished | ToolRoundCompleted


class ToolCallExecutor(Protocol):
    tools: ToolCatalogSnapshot

    def permission_snapshot(self) -> PermissionPolicy: ...

    def present_use(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
    ) -> ToolUsePresentation: ...

    def present_error(
        self,
        call: ToolCall,
        message: str,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
    ) -> ToolResultPresentation: ...

    def cancelled_result(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
    ) -> ToolResult: ...

    def is_concurrency_safe(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
    ) -> bool: ...

    async def execute(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
        permission_policy: PermissionPolicy | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> ToolExecutionOutcome: ...

    def apply_session_updates(
        self,
        updates: tuple[PermissionUpdate, ...],
        session_mode_writer: Callable[[PermissionMode], object],
    ) -> None: ...


class ToolRoundExecutor:
    """Execute safe call groups concurrently and close every protocol result."""

    def __init__(
        self,
        executor: ToolCallExecutor,
        *,
        max_parallel_calls: int = 1,
    ) -> None:
        if max_parallel_calls < 1:
            raise ValueError("max_parallel_calls must be positive")
        self.executor = executor
        self.max_parallel_calls = max_parallel_calls

    def permission_snapshot(self) -> PermissionPolicy | None:
        """Capture policy when supported, keeping injected test executors compatible."""

        snapshot = getattr(self.executor, "permission_snapshot", None)
        return snapshot() if snapshot is not None else None

    async def run_round(
        self,
        calls: tuple[ToolCall, ...],
        assistant_message: AssistantMessage,
        *,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot | None = None,
        permission_policy: PermissionPolicy | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> AsyncIterator[ToolRoundEvent]:
        active_tools = self.executor.tools if tools is None else tools
        results: list[ToolResult] = []
        attachments: list[AttachmentPayload] = []
        permission_updates: list[PermissionUpdate] = []
        try:
            for group in _execution_groups(calls, active_tools, self.executor):
                for call in group:
                    yield ToolCallStarted(
                        call,
                        self.executor.present_use(call, tools=active_tools),
                    )
                outcomes = await self._execute_group(
                    group,
                    active_tools,
                    permission_policy=permission_policy,
                    run_id=run_id,
                    session_id=session_id,
                    root_session_id=root_session_id,
                )
                for call, outcome in zip(group, outcomes, strict=True):
                    results.append(outcome.result)
                    if not outcome.result.is_error:
                        attachments.extend(outcome.new_attachments)
                        permission_updates.extend(outcome.permission_updates)
                    yield ToolCallFinished(
                        call=call,
                        result=outcome.result,
                        presentation=outcome.result.presentation,
                        new_attachments=outcome.new_attachments,
                        permission_updates=outcome.permission_updates,
                    )
        except asyncio.CancelledError:
            # 对尚未产生结果的调用逐一补齐稳定错误。先发出事件，Agent 可以
            # 在重新抛出取消前把整条 user tool-result 消息持久化。
            completed_ids = {result.tool_use_id for result in results}
            for call in calls:
                if call.id in completed_ids:
                    continue
                result = self.executor.cancelled_result(call, tools=active_tools)
                results.append(result)
                yield ToolCallFinished(
                    call=call,
                    result=result,
                    presentation=result.presentation,
                )
            yield ToolRoundCompleted(
                message=_tool_result_message(assistant_message, tuple(results)),
                new_attachments=tuple(attachments),
                permission_updates=tuple(permission_updates),
                cancelled=True,
            )
            raise

        yield ToolRoundCompleted(
            message=_tool_result_message(assistant_message, tuple(results)),
            new_attachments=_merge_discovery_attachments(attachments),
            permission_updates=tuple(permission_updates),
        )

    def apply_permission_updates(
        self,
        updates: tuple[PermissionUpdate, ...],
        session_mode_writer: Callable[[PermissionMode], object],
    ) -> None:
        self.executor.apply_session_updates(updates, session_mode_writer)

    async def _execute_group(
        self,
        calls: tuple[ToolCall, ...],
        tools: ToolCatalogSnapshot | ToolExposureSnapshot,
        *,
        permission_policy: PermissionPolicy | None,
        run_id: str | None,
        session_id: str | None,
        root_session_id: str | None,
    ) -> tuple[ToolExecutionOutcome, ...]:
        semaphore = asyncio.Semaphore(self.max_parallel_calls)

        async def execute(call: ToolCall) -> ToolExecutionOutcome:
            async with semaphore:
                try:
                    execute_call = self.executor.execute
                    if permission_policy is not None:
                        return await execute_call(
                            call,
                            tools=tools,
                            permission_policy=permission_policy,
                            run_id=run_id,
                            session_id=session_id,
                            root_session_id=root_session_id,
                        )
                    return await execute_call(
                        call,
                        tools=tools,
                        run_id=run_id,
                        session_id=session_id,
                        root_session_id=root_session_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # ToolExecutor 的正常失败已经会返回 result；这里覆盖适配器
                    # 外部注入的执行器异常，仍然保持协议闭合。
                    message = (
                        f"Unexpected {type(error).__name__} while executing {call.name}"
                    )
                    return ToolExecutionOutcome(
                        result=ToolResult(
                            tool_use_id=call.id,
                            content=message,
                            presentation=self.executor.present_error(
                                call, message, tools=tools
                            ),
                            is_error=True,
                        ),
                    )

        tasks = tuple(asyncio.create_task(execute(call)) for call in calls)
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def _execution_groups(
    calls: tuple[ToolCall, ...],
    tools: ToolCatalogSnapshot | ToolExposureSnapshot,
    executor: ToolCallExecutor,
) -> tuple[tuple[ToolCall, ...], ...]:
    groups: list[tuple[ToolCall, ...]] = []
    safe: list[ToolCall] = []
    for call in calls:
        concurrency_safe = executor.is_concurrency_safe(call, tools=tools)
        if concurrency_safe:
            safe.append(call)
            continue
        if safe:
            groups.append(tuple(safe))
            safe = []
        groups.append((call,))
    if safe:
        groups.append(tuple(safe))
    return tuple(groups)


def _tool_result_message(
    assistant_message: AssistantMessage,
    results: tuple[ToolResult, ...],
) -> ToolResultBatch:
    if not results:
        raise ValueError("A tool round must contain at least one result")
    return ToolResultBatch(
        content=results,
        parent_uuid=assistant_message.uuid,
        source_assistant_id=assistant_message.uuid,
    )


def _merge_discovery_attachments(
    attachments: list[AttachmentPayload],
) -> tuple[AttachmentPayload, ...]:
    ordinary: list[AttachmentPayload] = []
    definitions = {}
    mode: str | None = None
    for attachment in attachments:
        if isinstance(attachment, ToolDiscoveryAttachment):
            mode = attachment.mode
            definitions.update((item.name, item) for item in attachment.definitions)
        else:
            ordinary.append(attachment)
    if definitions:
        assert mode in {"dispatcher", "native"}
        ordinary.append(
            ToolDiscoveryAttachment(
                tuple(definitions[name] for name in sorted(definitions)), mode
            )
        )
    return tuple(ordinary)


__all__ = [
    "ToolCallFinished",
    "ToolCallExecutor",
    "ToolCallStarted",
    "ToolRoundCompleted",
    "ToolRoundEvent",
    "ToolRoundExecutor",
]
