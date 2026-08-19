"""nano-code 自带的内置工具。"""

from nano_code.tools.base import Tool
from nano_code.tools.builtin.bash import BashTool
from nano_code.tools.builtin.edit_file import EditFileTool
from nano_code.tools.builtin.glob_files import GlobTool
from nano_code.tools.builtin.grep_files import GrepTool
from nano_code.tools.builtin.read_file import ReadFileTool
from nano_code.tools.builtin.write_file import WriteFileTool


def builtin_tools() -> tuple[Tool, ...]:
    """在注册表校验前按稳定顺序返回内置工具。"""

    return (
        BashTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        ReadFileTool(),
        WriteFileTool(),
    )


__all__ = ["builtin_tools"]
