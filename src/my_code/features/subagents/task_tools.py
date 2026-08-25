"""Model-visible inspection and cancellation tools for background Subagents."""

import json

from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.models import SubagentParentContext
from my_code.features.subagents.serialization import background_subagent_payload
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import Tool, ToolContext, ToolInputError, ToolOutput


class TaskListTool(Tool):
    def __init__(
        self,
        controller: SubagentController,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.controller = controller
        self.parent = parent

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskList",
            "List background Subagent tasks started by this agent tree.",
            {"type": "object", "additionalProperties": False},
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
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
        context: ToolContext,
    ) -> ToolOutput:
        del tool_input
        tasks = self.controller.background_tasks(_owner(self.parent, context))
        return ToolOutput(
            json.dumps(
                {"tasks": [background_subagent_payload(item) for item in tasks]},
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


class TaskOutputTool(Tool):
    def __init__(
        self,
        controller: SubagentController,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.controller = controller
        self.parent = parent

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskOutput",
            "Get the current status and output of one background Subagent task.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return _allow(tool_input, "task-output")

    def validate_input(self, tool_input: JsonObject) -> None:
        _task_id(tool_input)

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        task = self.controller.background_task(
            _owner(self.parent, context),
            _task_id(tool_input),
        )
        return ToolOutput(
            json.dumps(background_subagent_payload(task), ensure_ascii=False)
        )

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return _task_id(tool_input)

    def present_result(
        self,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        return _present_task_result(tool_input, output)


class TaskCancelTool(Tool):
    def __init__(
        self,
        controller: SubagentController,
        *,
        parent: SubagentParentContext,
    ) -> None:
        self.controller = controller
        self.parent = parent

    @property
    def definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            "TaskCancel",
            "Cancel one non-terminal background Subagent task.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
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
        context: ToolContext,
    ) -> ToolOutput:
        task = await self.controller.cancel_background(
            _owner(self.parent, context),
            _task_id(tool_input),
        )
        return ToolOutput(
            json.dumps(background_subagent_payload(task), ensure_ascii=False)
        )

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        return _task_id(tool_input)

    def present_result(
        self,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        return _present_task_result(tool_input, output)


def _owner(parent: SubagentParentContext, context: ToolContext) -> str:
    run_id = context.run_id or parent.run_id
    return run_id if parent.depth == 0 else parent.owner_run_id


def _task_id(tool_input: JsonObject) -> str:
    value = tool_input.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError("task_id must be a non-empty string")
    return value


def _allow(tool_input: JsonObject, detail: str) -> ToolPermissionResult:
    return ToolPermissionResult.allow(
        tool_input,
        message="Background task metadata is read-only.",
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


__all__ = ["TaskCancelTool", "TaskListTool", "TaskOutputTool"]
