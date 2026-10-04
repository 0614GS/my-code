"""Provider-neutral MCP configuration, discovery, and lifecycle values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from my_code.foundation.json import JsonObject, to_json_object

_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REMOTE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PUBLIC_TOOL_NAME_LENGTH = 64


class McpServerScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


class McpServerTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpAuthKind(StrEnum):
    NONE = "none"
    AUTO = "auto"
    BEARER = "bearer"
    OAUTH = "oauth"


class McpAuthChallenge(StrEnum):
    OAUTH = "oauth"
    BEARER = "bearer"
    CHOOSE = "choose"


class McpConnectionState(StrEnum):
    DISABLED = "disabled"
    PENDING = "pending"
    CONNECTED = "connected"
    FAILED = "failed"
    AUTH_REQUIRED = "auth_required"
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
    AUTH_REQUIRED = "auth_required"


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """已解析的 server 配置；这里仅保存凭据来源，不保存凭据值。"""

    name: str
    command: str | None
    cwd: Path
    args: tuple[str, ...] = ()
    env_from: tuple[tuple[str, str], ...] = ()
    scope: McpServerScope = McpServerScope.USER
    enabled: bool = True
    start_allowed: bool = True
    startup_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 60.0
    transport: McpServerTransport = McpServerTransport.STDIO
    url: str | None = None
    auth: McpAuthKind = McpAuthKind.NONE
    bearer_token_from: str | None = None

    def __post_init__(self) -> None:
        if _SERVER_NAME.fullmatch(self.name) is None:
            raise ValueError("MCP server name must match [a-z0-9][a-z0-9_-]{0,63}")
        if self.transport is McpServerTransport.STDIO:
            if (
                self.command is None
                or not self.command.strip()
                or "\x00" in self.command
            ):
                raise ValueError(
                    "MCP server command must be non-empty and contain no NUL"
                )
            if self.url is not None or self.auth is not McpAuthKind.NONE:
                raise ValueError("stdio MCP server cannot use URL or HTTP auth")
        elif self.command is not None or self.url is None:
            raise ValueError("HTTP MCP server requires URL and no command")
        if self.transport is McpServerTransport.HTTP:
            assert self.url is not None
            if self.args or self.env_from:
                raise ValueError(
                    "HTTP MCP server cannot use stdio arguments or environment"
                )
            parsed = urlsplit(self.url)
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (
                        parsed.scheme == "http"
                        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
            ):
                raise ValueError("HTTP MCP URL requires HTTPS or loopback HTTP")
        if self.auth is McpAuthKind.BEARER:
            if (
                self.bearer_token_from is not None
                and _ENVIRONMENT_NAME.fullmatch(self.bearer_token_from) is None
            ):
                raise ValueError(
                    "Bearer MCP source must be an environment variable name"
                )
        elif self.bearer_token_from is not None:
            raise ValueError("Bearer token source requires bearer auth")
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
    auth_challenge: McpAuthChallenge | None = None


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


def _bounded_public_name(value: str) -> str:
    if len(value) <= _MAX_PUBLIC_TOOL_NAME_LENGTH:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:12]
    prefix_length = _MAX_PUBLIC_TOOL_NAME_LENGTH - len(digest) - 2
    return f"{value[:prefix_length]}__{digest}"


__all__ = [
    "McpCallResult",
    "McpAuthKind",
    "McpAuthChallenge",
    "McpConnectionInfo",
    "McpConnectionState",
    "McpDiagnostic",
    "McpDiagnosticCode",
    "McpRemoteTool",
    "McpServerScope",
    "McpServerSnapshot",
    "McpServerSpec",
    "McpServerTransport",
    "public_tool_name",
    "validate_remote_tool_name",
]
