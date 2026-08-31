"""核心工具协议，刻意与 provider 和 UI 解耦。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from my_code.conversation.attachments import AttachmentPayload
from my_code.conversation.presentation import ToolResultPresentation
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import PermissionUpdate, PermissionUpdateDestination
from my_code.tools.presentation import (
    ToolUsePresentation,
    compact_text,
    tool_display_category,
)
from my_code.workspace.launcher import CommandLauncher, LocalCommandLauncher
from my_code.workspace.local import Workspace

if TYPE_CHECKING:
    from my_code.permissions.models import ToolPermissionContext, ToolPermissionResult

_EMPTY_TOOLS: Mapping[str, Tool] = MappingProxyType({})


class ToolExposure(StrEnum):
    """Whether a tool is sent eagerly or must first be discovered."""

    EAGER = "eager"
    SEARCHABLE = "searchable"


@dataclass(frozen=True, slots=True, init=False)
class ToolExecutionContext:
    """内置工具可用的运行时依赖。"""

    workspace: Workspace
    command_launcher: CommandLauncher
    command_timeout_seconds: float
    max_command_output_bytes: int
    available_tools: Mapping[str, Tool]
    tool_snapshot_version: int | None
    run_id: str | None
    session_id: str | None
    root_session_id: str | None
    tool_use_id: str | None
    internal_read_root: Path | None
    searched_fingerprints: Mapping[str, str]

    def __init__(
        self,
        workspace: Workspace | Path,
        command_timeout_seconds: float = 120.0,
        max_command_output_bytes: int = 4 * 1024 * 1024,
        available_tools: Mapping[str, Tool] = _EMPTY_TOOLS,
        tool_snapshot_version: int | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
        tool_use_id: str | None = None,
        internal_read_root: Path | None = None,
        searched_fingerprints: Mapping[str, str] = MappingProxyType({}),
        *,
        command_launcher: CommandLauncher | None = None,
    ) -> None:
        object.__setattr__(
            self,
            "workspace",
            workspace if isinstance(workspace, Workspace) else Workspace(workspace),
        )
        object.__setattr__(
            self, "command_launcher", command_launcher or LocalCommandLauncher()
        )
        object.__setattr__(self, "command_timeout_seconds", command_timeout_seconds)
        object.__setattr__(self, "max_command_output_bytes", max_command_output_bytes)
        object.__setattr__(
            self,
            "available_tools",
            MappingProxyType(dict(available_tools)),
        )
        object.__setattr__(self, "tool_snapshot_version", tool_snapshot_version)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "root_session_id", root_session_id)
        object.__setattr__(self, "tool_use_id", tool_use_id)
        object.__setattr__(
            self,
            "internal_read_root",
            internal_read_root.resolve(strict=False)
            if internal_read_root is not None
            else None,
        )
        object.__setattr__(
            self,
            "searched_fingerprints",
            MappingProxyType(dict(searched_fingerprints)),
        )

    @property
    def cwd(self) -> Path:
        return self.workspace.root

    def with_tools(
        self,
        tools: Mapping[str, Tool],
        *,
        version: int,
        run_id: str | None = None,
        session_id: str | None = None,
        root_session_id: str | None = None,
        tool_use_id: str | None = None,
        searched_fingerprints: Mapping[str, str] = MappingProxyType({}),
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            self.workspace,
            self.command_timeout_seconds,
            self.max_command_output_bytes,
            tools,
            version,
            run_id,
            session_id,
            root_session_id,
            tool_use_id,
            self.internal_read_root,
            searched_fingerprints,
            command_launcher=self.command_launcher,
        )


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """工具实现返回的 provider 无关输出。"""

    content: str
    is_error: bool = False
    metadata: JsonObject = field(default_factory=dict)
    new_attachments: tuple[AttachmentPayload, ...] = ()
    permission_updates: tuple[PermissionUpdate, ...] = ()

    def __post_init__(self) -> None:
        if self.is_error and (self.new_attachments or self.permission_updates):
            raise ValueError("Failed tools cannot produce follow-up state")
        if any(
            update.destination is not PermissionUpdateDestination.SESSION
            for update in self.permission_updates
        ):
            raise ValueError("Tool outputs may update session permissions only")


class ToolInputError(ValueError):
    """输入未通过 schema 相关或语义校验时抛出。"""


class ToolExecutionError(RuntimeError):
    """合法工具请求无法完成时抛出。"""


@dataclass(frozen=True, slots=True)
class ReadOnlyAssessment:
    """Explain whether one concrete invocation is statically read-only."""

    is_read_only: bool
    reason: str


class Tool(ABC):
    """封装校验、权限元数据与执行的强类型单元。"""

    @property
    @abstractmethod
    def definition(self) -> ModelToolDefinition:
        """返回模型可见的定义。"""

    @property
    def exposure(self) -> ToolExposure:
        """Declare how the tool becomes available to the model."""

        return ToolExposure.EAGER

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        """Return whether this specific invocation may overlap other calls."""

        del tool_input
        return False

    def user_facing_name(self, tool_input: JsonObject) -> str:
        """返回面向用户的稳定工具名称。"""

        del tool_input
        return self.definition.name

    def get_tool_use_summary(self, tool_input: JsonObject) -> str:
        """返回紧凑视图中的调用摘要。"""

        import json

        return compact_text(
            json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
        )

    def get_activity_description(self, tool_input: JsonObject) -> str:
        """返回工具执行期间的活动说明。"""

        return f"Running {self.user_facing_name(tool_input)}"

    def present_use(self, tool_input: JsonObject) -> ToolUsePresentation:
        """组合供任意前端消费的工具调用展示数据。"""

        return ToolUsePresentation(
            display_name=self.user_facing_name(tool_input),
            summary=self.get_tool_use_summary(tool_input),
            activity=self.get_activity_description(tool_input),
            category=tool_display_category(self.definition.name),
        )

    def present_result(
        self, tool_input: JsonObject, output: ToolOutput
    ) -> ToolResultPresentation:
        """将执行结果投影为前端无关的紧凑展示。"""

        del tool_input
        lines = [line.strip() for line in output.content.splitlines() if line.strip()]
        if not lines:
            summary = (
                "failed with no details"
                if output.is_error
                else "completed with no output"
            )
        else:
            summary = lines[0]
        return ToolResultPresentation(summary=compact_text(summary))

    def present_error(
        self, tool_input: JsonObject, message: str
    ) -> ToolResultPresentation:
        """将校验、权限或执行错误投影为紧凑展示。"""

        del tool_input
        return ToolResultPresentation(summary=compact_text(message))

    def cancelled_output(self, tool_input: JsonObject) -> ToolOutput:
        """Project a user abort into a stable, model-visible error output."""

        del tool_input
        return ToolOutput("Tool execution was aborted by the user.", is_error=True)

    def to_model_result(self, output: ToolOutput) -> str:
        """把工具原始输出序列化为发送给模型的内容。"""

        return output.content

    @abstractmethod
    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        """描述当前具体调用的副作用语义。

        Bash 等动态工具会覆盖此方法。该元数据服务于权限、调度和 UI 代码，
        其本身不会授予访问权限。
        """

        raise NotImplementedError

    def assess_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ReadOnlyAssessment:
        read_only = self.is_read_only(tool_input, context)
        return ReadOnlyAssessment(
            read_only,
            "tool invocation is read-only"
            if read_only
            else "tool invocation is not proven read-only",
        )

    @abstractmethod
    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """返回供全局策略消费的工具局部判断。"""

        raise NotImplementedError

    @abstractmethod
    def validate_input(self, tool_input: JsonObject) -> None:
        """错误输入应在权限评估前抛出 ``ToolInputError``。"""

    @abstractmethod
    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        """执行已校验并获准的调用。"""


__all__ = [
    "ReadOnlyAssessment",
    "Tool",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolInputError",
    "ToolOutput",
    "ToolExposure",
]
