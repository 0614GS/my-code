"""核心工具协议，刻意与 provider 和 UI 解耦。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from nano_code.messages import JsonObject

if TYPE_CHECKING:
    from nano_code.permissions.models import (
        ToolPermissionContext,
        ToolPermissionResult,
    )


class ToolRisk(StrEnum):
    """权限引擎消费的副作用类别。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """暴露给模型的稳定工具标识和 schema。"""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ToolContext:
    """内置工具可用的运行时依赖。"""

    cwd: Path
    command_timeout_seconds: float = 120.0
    max_command_output_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """工具实现返回的 provider 无关输出。"""

    content: str
    is_error: bool = False


class ToolInputError(ValueError):
    """输入未通过 schema 相关或语义校验时抛出。"""


class ToolExecutionError(RuntimeError):
    """合法工具请求无法完成时抛出。"""


class Tool(ABC):
    """封装校验、权限元数据与执行的强类型单元。"""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """返回模型可见的定义。"""

    @property
    @abstractmethod
    def risk(self) -> ToolRisk:
        """返回默认副作用类别。"""

    @property
    def concurrency_safe(self) -> bool:
        """调度器支持并行后，该工具调用是否可以并行。"""

        return False

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        """描述当前具体调用的副作用语义。

        Bash 等动态工具会覆盖此方法。该元数据服务于权限、调度和 UI 代码，
        其本身不会授予访问权限。
        """

        del tool_input, context
        return self.risk is ToolRisk.READ

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """返回供全局策略消费的工具局部判断。"""

        # 局部导入可避免运行时循环：权限策略需要导入 Tool，
        # 而与 provider/UI 无关的协议这里只需要其类型。
        from nano_code.permissions.models import ToolPermissionResult

        if self.is_read_only(tool_input, context.tool_context):
            return ToolPermissionResult.allow(
                tool_input,
                message="This call is read-only.",
                reason="tool:read-only",
            )
        return ToolPermissionResult.passthrough(
            message=f"{self.definition.name} requires approval for this call.",
            reason="tool:passthrough",
        )

    @abstractmethod
    def validate_input(self, tool_input: JsonObject) -> None:
        """错误输入应在权限评估前抛出 ``ToolInputError``。"""

    @abstractmethod
    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        """执行已校验并获准的调用。"""
