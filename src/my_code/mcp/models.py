"""Provider-neutral MCP configuration, discovery, and lifecycle values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from my_code.foundation.json import JsonObject, to_json_object

_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REMOTE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PUBLIC_TOOL_NAME_LENGTH = 64


class McpServerScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


class McpConnectionState(StrEnum):
    DISABLED = "disabled"
    PENDING = "pending"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


class McpDiagnosticCode(StrEnum):
    GATE_DISABLED = "gate_disabled"
    SERVER_DISABLED = "server_disabled"
    PROJECT_NOT_TRUSTED = "project_not_trusted"
    CONFIGURATION_ERROR = "configuration_error"
    START_FAILED = "start_failed"
    DISCOVERY_FAILED = "discovery_failed"
    REGISTRATION_FAILED = "registration_failed"
    CONNECTION_LOST = "connection_lost"


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """Resolved subprocess configuration; secret values are never persisted here."""

    name: str
    command: str
    cwd: Path
    args: tuple[str, ...] = ()
    env_from: tuple[tuple[str, str], ...] = ()
    scope: McpServerScope = McpServerScope.USER
    enabled: bool = True
    start_allowed: bool = True
    startup_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if _SERVER_NAME.fullmatch(self.name) is None:
            raise ValueError("MCP server name must match [a-z0-9][a-z0-9_-]{0,63}")
        if not self.command.strip() or "\x00" in self.command:
            raise ValueError("MCP server command must be non-empty and contain no NUL")
        if any("\x00" in argument for argument in self.args):
            raise ValueError("MCP server arguments must not contain NUL")
        if self.startup_timeout_seconds <= 0 or self.call_timeout_seconds <= 0:
            raise ValueError("MCP server timeouts must be positive")
        targets: set[str] = set()
        for target, source in self.env_from:
            if (
                _ENVIRONMENT_NAME.fullmatch(target) is None
                or _ENVIRONMENT_NAME.fullmatch(source) is None
            ):
                raise ValueError("MCP environment references must be variable names")
            if target in targets:
                raise ValueError(f"Duplicate MCP target environment variable: {target}")
            targets.add(target)


@dataclass(frozen=True, slots=True)
class McpRemoteTool:
    name: str
    description: str
    input_schema: JsonObject

    def __post_init__(self) -> None:
        validate_remote_tool_name(self.name)
        object.__setattr__(self, "input_schema", to_json_object(self.input_schema))


@dataclass(frozen=True, slots=True)
class McpCallResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class McpConnectionInfo:
    protocol_version: str
    server_name: str
    server_version: str


@dataclass(frozen=True, slots=True)
class McpDiagnostic:
    server: str
    state: McpConnectionState
    code: McpDiagnosticCode
    message: str


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    name: str
    state: McpConnectionState
    tool_names: tuple[str, ...]
    diagnostic: McpDiagnostic | None
    connection_info: McpConnectionInfo | None


@dataclass(frozen=True, slots=True)
class McpSearchMatch:
    server_name: str
    remote_name: str
    public_name: str
    description: str


def validate_remote_tool_name(name: str) -> str:
    if _REMOTE_TOOL_NAME.fullmatch(name) is None:
        raise ValueError("MCP tool name must match [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
    return name


def public_tool_name(server_name: str, remote_name: str) -> str:
    """Map a remote identity to a provider-portable stable namespace."""

    if _SERVER_NAME.fullmatch(server_name) is None:
        raise ValueError("Invalid MCP server name")
    validate_remote_tool_name(remote_name)
    normalized = remote_name.replace(".", "_dot_")
    return _bounded_public_name(f"mcp__{server_name}__{normalized}")


def tool_search_name(server_name: str) -> str:
    if _SERVER_NAME.fullmatch(server_name) is None:
        raise ValueError("Invalid MCP server name")
    return _bounded_public_name(f"mcp_search__{server_name}")


def _bounded_public_name(value: str) -> str:
    if len(value) <= _MAX_PUBLIC_TOOL_NAME_LENGTH:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:12]
    prefix_length = _MAX_PUBLIC_TOOL_NAME_LENGTH - len(digest) - 2
    return f"{value[:prefix_length]}__{digest}"


__all__ = [
    "McpCallResult",
    "McpConnectionInfo",
    "McpConnectionState",
    "McpDiagnostic",
    "McpDiagnosticCode",
    "McpRemoteTool",
    "McpSearchMatch",
    "McpServerScope",
    "McpServerSnapshot",
    "McpServerSpec",
    "public_tool_name",
    "tool_search_name",
    "validate_remote_tool_name",
]
