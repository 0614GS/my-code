"""Model-visible inspection and cancellation tools for all background tasks."""

import json

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
        return ToolOutput(
            json.dumps(
                {"tasks": [self.registry.payload(item) for item in tasks]},
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


__all__ = ["TaskCancelTool", "TaskListTool"]
