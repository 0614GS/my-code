"""Model-visible inspection and cancellation tools for all background tasks."""

import json

from my_code.conversation.attachments import BackgroundTaskCompletionAttachment
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.background_tasks.registry import BackgroundTaskRegistry
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.models import SubagentParentContext
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolExposure,
    ToolInputError,
    ToolOutput,
)


class TaskListTool(Tool):
    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    def __init__(
        self,
        controller: SubagentController | BackgroundTaskRegistry,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.registry = (
            controller.background_registry
            if isinstance(controller, SubagentController)
            else controller
        )
        self.parent = parent

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskList",
            "List background Bash and Subagent tasks started by this agent tree.",
            {"type": "object", "additionalProperties": False},
        )

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return _allow(tool_input, "task-list")

    def validate_input(self, tool_input: JsonObject) -> None:
        if tool_input:
            raise ToolInputError("TaskList accepts no input")

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del tool_input
        owner = _owner(self.parent, context)
        tasks = self.registry.tasks_for(owner)
        remaining = 8192
        payloads = []
        for item in tasks:
            payload = self.registry.payload(item)
            if item.task_type == "bash":
                payload.update(self.registry.output_preview(item, terminal=False))
            preview = payload.get("output_preview")
            if isinstance(preview, str):
                encoded = preview.encode("utf-8")
                if len(encoded) > remaining:
                    payload["output_preview"] = (
                        encoded[-remaining:].decode("utf-8", errors="ignore")
                        if remaining
                        else ""
                    )
                    payload["output_truncated"] = True
                remaining = max(0, remaining - len(encoded))
            payloads.append(payload)
        return ToolOutput(
            json.dumps(
                {"tasks": payloads},
                ensure_ascii=False,
            )
        )

    def present_result(
        self,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        del tool_input
        try:
            payload = json.loads(output.content)
            count = len(payload.get("tasks", ())) if isinstance(payload, dict) else 0
        except (json.JSONDecodeError, TypeError):
            return super().present_result({}, output)
        return ToolResultPresentation(summary=f"Listed {count} background tasks")


class TaskCancelTool(Tool):
    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    def __init__(
        self,
        controller: SubagentController | BackgroundTaskRegistry,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.registry = (
            controller.background_registry
            if isinstance(controller, SubagentController)
            else controller
        )
        self.parent = parent

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskCancel",
            "Cancel one non-terminal background Bash or Subagent task.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.ask(
            message="Cancelling a background task requires confirmation.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL,
                "task-cancel",
            ),
            updated_input=tool_input,
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        _task_id(tool_input)

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        owner = _owner(self.parent, context)
        item = await self.registry.cancel(owner, _task_id(tool_input))
        return ToolOutput(json.dumps(self.registry.payload(item), ensure_ascii=False))

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return _task_id(tool_input)

    def present_result(
        self,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        return _present_task_result(tool_input, output)


class TaskWaitTool(Tool):
    def __init__(
        self,
        controller: SubagentController | BackgroundTaskRegistry,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.registry = (
            controller.background_registry
            if isinstance(controller, SubagentController)
            else controller
        )
        self.parent = parent

    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskWait",
            "Wait for one background task, a user message, or timeout. "
            "Returns bounded new output; timeout does not cancel the task.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 5, "maximum": 300},
                    "output_offset": {"type": "integer", "minimum": 0},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return _allow(tool_input, "task-wait")

    def validate_input(self, tool_input: JsonObject) -> None:
        _task_id(tool_input)
        timeout = tool_input.get("timeout_seconds", 30)
        offset = tool_input.get("output_offset", 0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 5 <= timeout <= 300
        ):
            raise ToolInputError("timeout_seconds must be between 5 and 300")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ToolInputError("output_offset must be a non-negative integer")

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        self.validate_input(tool_input)
        owner = _owner(self.parent, context)
        task_id = _task_id(tool_input)
        item = self.registry.get(owner, task_id)
        timeout = tool_input.get("timeout_seconds", 30)
        offset = tool_input.get("output_offset", 0)
        assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        assert isinstance(offset, int) and not isinstance(offset, bool)
        reason = await self.registry.wait(owner, task_id, float(timeout))
        payload = self.registry.payload(item)
        payload.pop("output_preview", None)
        payload.pop("output_truncated", None)
        payload.update(self.registry.output_since(item, offset))
        payload["end_reason"] = reason
        attachments = (
            (
                BackgroundTaskCompletionAttachment(
                    owner, task_id, self.registry.payload(item)
                ),
            )
            if reason == "completed"
            and self.registry.tasks.snapshot(task_id).status.terminal
            and item in self.registry.pending(owner)
            else ()
        )
        return ToolOutput(
            json.dumps(payload, ensure_ascii=False), new_attachments=attachments
        )

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return _task_id(tool_input)

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        return _present_task_result(tool_input, output)


class TaskWatchTool(TaskWaitTool):
    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskWatch",
            "Subscribe or unsubscribe from one completion-triggered idle "
            "continuation. Completed tasks are delivered on the next model request.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        _task_id(tool_input)
        if not isinstance(tool_input.get("enabled", True), bool):
            raise ToolInputError("enabled must be a boolean")

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return _allow(tool_input, "task-watch")

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        self.validate_input(tool_input)
        owner = _owner(self.parent, context)
        item = self.registry.get(owner, _task_id(tool_input))
        watching = self.registry.watch(
            owner, item.task_id, enabled=bool(tool_input.get("enabled", True))
        )
        payload: JsonObject = {
            "task_id": item.task_id,
            "status": self.registry.tasks.snapshot(item.task_id).status.value,
            "watching": watching,
        }
        return ToolOutput(json.dumps(payload, ensure_ascii=False))


def _owner(parent: SubagentParentContext, context: ToolExecutionContext) -> str:
    owner = context.root_session_id or context.session_id
    if owner is not None:
        return owner
    if context.run_id is not None:
        return context.run_id if parent.depth == 0 else parent.owner_run_id
    return parent.owner_session_id


def _task_id(tool_input: JsonObject) -> str:
    value = tool_input.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError("task_id must be a non-empty string")
    return value


def _allow(tool_input: JsonObject, detail: str) -> ToolPermissionResult:
    return ToolPermissionResult.allow(
        tool_input,
        message="Background task operation is allowed.",
        reason=PermissionDecisionReason(PermissionDecisionKind.TOOL, detail),
    )


def _present_task_result(
    tool_input: JsonObject,
    output: ToolOutput,
) -> ToolResultPresentation:
    task_id = _task_id(tool_input)
    try:
        payload = json.loads(output.content)
        status = payload.get("status") if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        status = None
    return ToolResultPresentation(summary=f"Task {task_id}: {status or 'unknown'}")


__all__ = ["TaskCancelTool", "TaskListTool", "TaskWaitTool", "TaskWatchTool"]
