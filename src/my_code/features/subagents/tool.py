"""Model-visible foreground Subagent Tool adapter."""

import json

from my_code.agent.models import AgentMaxStepsReached, AgentTurnSucceeded
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.models import SubagentParentContext, SubagentSpec
from my_code.model.primitives import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.tasks.models import TaskStatus
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolInputError,
    ToolOutput,
)


class SubagentTool(Tool):
    def __init__(
        self,
        controller: SubagentController,
        *,
        parent: SubagentParentContext,
        policy: PermissionPolicy,
        allow_background: bool | None = None,
    ) -> None:
        self.controller = controller
        self.parent = parent
        self.policy = policy
        self.allow_background = (
            controller.background_enabled
            if allow_background is None
            else allow_background
        )

    @property
    def definition(self) -> ModelToolDefinition:
        input_schema: JsonObject = {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short stable task description",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete instruction for the child agent",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": "Optional subset of currently available tools",
                },
            },
            "required": ["description", "prompt"],
            "additionalProperties": False,
        }
        properties = input_schema["properties"]
        assert isinstance(properties, dict)
        if self.allow_background:
            properties["background"] = {
                "type": "boolean",
                "description": "Return a task ID immediately instead of waiting",
                "default": False,
            }
        return ModelToolDefinition(
            name="Subagent",
            description=(
                "Run an isolated child coding agent. The child has its own Session "
                "and a restricted tool snapshot."
            ),
            input_schema=input_schema,
        )

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return False

    def validate_input(self, tool_input: JsonObject) -> None:
        for key in ("description", "prompt"):
            if (
                not isinstance(tool_input.get(key), str)
                or not str(tool_input[key]).strip()
            ):
                raise ToolInputError(f"{key} must be a non-empty string")
        tools = tool_input.get("tools")
        if tools is not None:
            if (
                not isinstance(tools, list)
                or not tools
                or any(not isinstance(name, str) or not name.strip() for name in tools)
                or len(tools) != len(set(tools))
            ):
                raise ToolInputError("tools must contain unique non-empty names")
        background = tool_input.get("background", False)
        if not isinstance(background, bool):
            raise ToolInputError("background must be a boolean")
        if background and not self.allow_background:
            raise ToolInputError("background Subagents are disabled")

    async def check_permissions(
        self,
        tool_input: JsonObject,
        context: ToolPermissionContext,
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.ask(
            message="Starting a child agent requires confirmation.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "subagent-spawn"
            ),
            updated_input=tool_input,
        )

    async def execute(
        self,
        tool_input: JsonObject,
        context: ToolContext,
    ) -> ToolOutput:
        tools = tool_input.get("tools")
        allowed_tools: tuple[str, ...] | None = None
        if isinstance(tools, list):
            if not all(isinstance(name, str) for name in tools):
                raise ToolInputError("tools must contain strings")
            allowed_tools = tuple(str(name) for name in tools)
        snapshot_version = context.tool_snapshot_version
        if snapshot_version is None:
            raise RuntimeError("Subagent requires an active tool snapshot")
        spec = SubagentSpec(
            prompt=str(tool_input["prompt"]),
            description=str(tool_input["description"]),
            allowed_tools=allowed_tools,
        )
        parent = self._runtime_parent(context)
        if tool_input.get("background") is True:
            started, _ = await self.controller.start(
                spec,
                parent=parent,
                parent_policy=self.policy,
                available_tools=context.available_tools,
                tool_snapshot_version=snapshot_version,
                background=True,
            )
            return ToolOutput(
                json.dumps(
                    {
                        "status": "started",
                        "task_id": started.task_id,
                        "run_id": started.run_id,
                    },
                    ensure_ascii=False,
                )
            )
        completed = await self.controller.run_foreground(
            spec,
            parent=parent,
            parent_policy=self.policy,
            available_tools=context.available_tools,
            tool_snapshot_version=snapshot_version,
        )
        task = completed.task
        if task.status is not TaskStatus.SUCCEEDED:
            failure = task.failure
            payload = {
                "status": task.status.value,
                "task_id": task.task_id,
                "run_id": completed.run_id,
                "error": failure.message if failure is not None else "unknown failure",
                "error_kind": failure.kind if failure is not None else "unknown",
            }
            return ToolOutput(json.dumps(payload, ensure_ascii=False), is_error=True)
        outcome = completed.outcome
        if isinstance(outcome, AgentMaxStepsReached):
            return ToolOutput(
                json.dumps(
                    {
                        "status": "max_steps",
                        "task_id": task.task_id,
                        "run_id": completed.run_id,
                        "completed_steps": outcome.completed_steps,
                        "max_steps": outcome.max_steps,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )
        if not isinstance(outcome, AgentTurnSucceeded):
            raise RuntimeError("Subagent completed without a valid outcome")
        return ToolOutput(
            json.dumps(
                {
                    "status": "succeeded",
                    "task_id": task.task_id,
                    "run_id": completed.run_id,
                    "result": outcome.text,
                    "completed_steps": outcome.completed_steps,
                },
                ensure_ascii=False,
            )
        )

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        description = tool_input.get("description")
        return description if isinstance(description, str) else "child agent"

    def _runtime_parent(self, context: ToolContext) -> SubagentParentContext:
        run_id = context.run_id or self.parent.run_id
        root_run_id = run_id if self.parent.depth == 0 else self.parent.owner_run_id
        return SubagentParentContext(
            run_id,
            self.parent.depth,
            self.parent.task_id,
            root_run_id,
        )

    def get_activity_description(self, tool_input: JsonObject) -> str:
        return f"Running subagent: {self.get_tool_use_summary(tool_input)}"

    def present_result(
        self,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        del tool_input
        try:
            payload = json.loads(output.content)
        except json.JSONDecodeError:
            return super().present_result({}, output)
        status = payload.get("status") if isinstance(payload, dict) else None
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if status == "started" and isinstance(task_id, str):
            return ToolResultPresentation(summary=f"Subagent started: {task_id}")
        return ToolResultPresentation(summary=f"Subagent {status or 'completed'}")


__all__ = ["SubagentTool"]
