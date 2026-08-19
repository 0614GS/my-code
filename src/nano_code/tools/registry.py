"""稳定的工具注册与查找。"""

from collections.abc import Iterable

from nano_code.model.request import ModelToolDefinition
from nano_code.tools.base import Tool


class ToolRegistry:
    """不可变且顺序确定的工具集合。"""

    def __init__(self, tools: Iterable[Tool]) -> None:
        # 工具 schema 顺序是提示前缀的一部分。稳定排序可提高 provider 缓存复用率，
        # 并使快照结果具有确定性。
        ordered = sorted(tools, key=lambda tool: tool.definition.name)
        by_name: dict[str, Tool] = {}
        for tool in ordered:
            name = tool.definition.name
            if name in by_name:
                # 遇到重名时失败而非遮蔽工具：权限规则和模型调用必须将名称
                # 解析到唯一实现。
                raise ValueError(f"Duplicate tool name: {name}")
            by_name[name] = tool
        self._tools = tuple(ordered)
        self._by_name = by_name

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    @property
    def definitions(self) -> tuple[ModelToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools)

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)


__all__ = [
    "ToolRegistry",
]
