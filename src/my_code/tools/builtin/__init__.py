"""my-code 自带的内置工具。"""

from my_code.tools.base import Tool
from my_code.tools.builtin.bash import BashTool
from my_code.tools.builtin.bash.tool import BashBackgroundExecutor
from my_code.tools.builtin.edit_file import EditFileTool
from my_code.tools.builtin.glob_files import GlobTool
from my_code.tools.builtin.grep_files import GrepTool
from my_code.tools.builtin.read_file import ReadFileTool
from my_code.tools.builtin.write_file import WriteFileTool


def builtin_tools(
    *,
    bash_background: BashBackgroundExecutor | None = None,
    background_enabled: bool = False,
    execution_environment: str = "local",
    sandboxed: bool = False,
    escalation_enabled: bool = False,
) -> tuple[Tool, ...]:
    """在注册表校验前按稳定顺序返回内置工具。"""

    return (
        BashTool(
            background_executor=bash_background,
            background_enabled=background_enabled,
            execution_environment=execution_environment,
            sandboxed=sandboxed,
            escalation_enabled=escalation_enabled,
        ),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        ReadFileTool(),
        WriteFileTool(),
    )


__all__ = [
    "builtin_tools",
]
