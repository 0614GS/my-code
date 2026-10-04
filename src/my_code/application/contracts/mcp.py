"""前端提交 MCP 连接配置时使用的无凭据值输入。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class McpServerRegistration:
    name: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env_from: tuple[tuple[str, str], ...] = ()
    url: str | None = None
    auth: str = "none"
    bearer_token_from: str | None = None


__all__ = ["McpServerRegistration"]
