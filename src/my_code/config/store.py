"""Layered v3 JSON settings with strict known-field validation."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.model.primitives import validate_provider_id
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import PermissionMode
from my_code.permissions.rules import (
    permission_rule_to_string,
    validate_permission_rule,
)

_SCHEMA_VERSION = 3
_MCP_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SettingsFileError(ValueError):
    """A settings file exists but cannot be interpreted safely."""


class SandboxMode(StrEnum):
    AUTO = "auto"
    LOCAL = "local"


class SandboxNetwork(StrEnum):
    RESTRICTED = "restricted"
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class SandboxSettingsLayer:
    mode: SandboxMode | None = None
    network: SandboxNetwork | None = None
    allow_unsandboxed_commands: bool | None = None


@dataclass(frozen=True, slots=True)
class AgentSettingsLayer:
    max_steps: int | None = None
    max_output_tokens: int | None = None
    context_chars: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionSettingsLayer:
    default_mode: PermissionMode | None = None
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSettingsLayer:
    max_parallel_calls: int | None = None
    tool_search_mode: ToolSearchMode | None = None


@dataclass(frozen=True, slots=True)
class SubagentSettingsLayer:
    enabled: bool | None = None
    max_depth: int | None = None
    max_active_children: int | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BackgroundTaskSettingsLayer:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class SkillSettingsLayer:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class McpServerSettingsLayer:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env_from: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    startup_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 60.0
    scope: SettingsScope = SettingsScope.USER


@dataclass(frozen=True, slots=True)
class McpSettingsLayer:
    enabled: bool | None = None
    servers: tuple[McpServerSettingsLayer, ...] = ()


@dataclass(frozen=True, slots=True, init=False)
class SettingsLayer:
    """A partial settings layer; nested values mirror the on-disk domains."""

    active_provider: str | None
    agent: AgentSettingsLayer
    permissions: PermissionSettingsLayer
    tools: ToolSettingsLayer
    subagents: SubagentSettingsLayer
    background_tasks: BackgroundTaskSettingsLayer
    skills: SkillSettingsLayer
    mcp: McpSettingsLayer
    sandbox: SandboxSettingsLayer

    def __init__(
        self,
        *,
        active_provider: str | None = None,
        agent: AgentSettingsLayer | None = None,
        permissions: PermissionSettingsLayer | None = None,
        tools: ToolSettingsLayer | None = None,
        subagents: SubagentSettingsLayer | None = None,
        background_tasks: BackgroundTaskSettingsLayer | None = None,
        skills: SkillSettingsLayer | None = None,
        mcp: McpSettingsLayer | None = None,
        sandbox: SandboxSettingsLayer | None = None,
        # Convenience aliases for callers while the disk schema remains nested.
        permission_mode: PermissionMode | None = None,
        permission_allow_rules: tuple[str, ...] = (),
        permission_deny_rules: tuple[str, ...] = (),
        permission_ask_rules: tuple[str, ...] = (),
        max_steps: int | None = None,
        max_output_tokens: int | None = None,
        context_chars: int | None = None,
        max_parallel_tool_calls: int | None = None,
        tool_search_mode: ToolSearchMode | None = None,
        subagents_enabled: bool | None = None,
        subagent_max_depth: int | None = None,
        subagent_max_active_children: int | None = None,
        subagent_max_steps: int | None = None,
        subagent_max_tokens: int | None = None,
        subagent_timeout_seconds: float | None = None,
        background_tasks_enabled: bool | None = None,
        skills_enabled: bool | None = None,
        mcp_enabled: bool | None = None,
        mcp_servers: tuple[McpServerSettingsLayer, ...] = (),
    ) -> None:
        if agent is not None and any(
            value is not None for value in (max_steps, max_output_tokens, context_chars)
        ):
            raise TypeError("agent and flattened agent values cannot be combined")
        if permissions is not None and (
            permission_mode is not None
            or permission_allow_rules
            or permission_deny_rules
            or permission_ask_rules
        ):
            raise TypeError(
                "permissions and flattened permission values cannot be combined"
            )
        if tools is not None and (
            max_parallel_tool_calls is not None or tool_search_mode is not None
        ):
            raise TypeError("tools and flattened tool values cannot be combined")
        if subagents is not None and any(
            value is not None
            for value in (
                subagents_enabled,
                subagent_max_depth,
                subagent_max_active_children,
                subagent_max_steps,
                subagent_max_tokens,
                subagent_timeout_seconds,
            )
        ):
            raise TypeError(
                "subagents and flattened subagent values cannot be combined"
            )
        if background_tasks is not None and background_tasks_enabled is not None:
            raise TypeError(
                "background_tasks and flattened background task values "
                "cannot be combined"
            )
        if skills is not None and skills_enabled is not None:
            raise TypeError("skills and flattened Skill values cannot be combined")
        if mcp is not None and (mcp_enabled is not None or mcp_servers):
            raise TypeError("mcp and flattened MCP values cannot be combined")
        object.__setattr__(self, "active_provider", active_provider)
        object.__setattr__(
            self,
            "agent",
            agent or AgentSettingsLayer(max_steps, max_output_tokens, context_chars),
        )
        object.__setattr__(
            self,
            "permissions",
            permissions
            or PermissionSettingsLayer(
                permission_mode,
                permission_allow_rules,
                permission_ask_rules,
                permission_deny_rules,
            ),
        )
        object.__setattr__(
            self,
            "background_tasks",
            background_tasks or BackgroundTaskSettingsLayer(background_tasks_enabled),
        )
        object.__setattr__(
            self,
            "skills",
            skills or SkillSettingsLayer(skills_enabled),
        )
        object.__setattr__(
            self,
            "tools",
            tools or ToolSettingsLayer(max_parallel_tool_calls, tool_search_mode),
        )
        object.__setattr__(
            self,
            "subagents",
            subagents
            or SubagentSettingsLayer(
                subagents_enabled,
                subagent_max_depth,
                subagent_max_active_children,
                subagent_max_steps,
                subagent_max_tokens,
                subagent_timeout_seconds,
            ),
        )
        object.__setattr__(
            self,
            "mcp",
            mcp
            or McpSettingsLayer(
                mcp_enabled,
                mcp_servers,
            ),
        )
        object.__setattr__(self, "sandbox", sandbox or SandboxSettingsLayer())

    @property
    def max_steps(self) -> int | None:
        return self.agent.max_steps

    @property
    def max_output_tokens(self) -> int | None:
        return self.agent.max_output_tokens

    @property
    def context_chars(self) -> int | None:
        return self.agent.context_chars

    @property
    def permission_mode(self) -> PermissionMode | None:
        return self.permissions.default_mode

    @property
    def permission_allow_rules(self) -> tuple[str, ...]:
        return self.permissions.allow

    @property
    def permission_ask_rules(self) -> tuple[str, ...]:
        return self.permissions.ask

    @property
    def permission_deny_rules(self) -> tuple[str, ...]:
        return self.permissions.deny

    @property
    def max_parallel_tool_calls(self) -> int | None:
        return self.tools.max_parallel_calls

    @property
    def tool_search_mode(self) -> ToolSearchMode | None:
        return self.tools.tool_search_mode

    @property
    def subagents_enabled(self) -> bool | None:
        return self.subagents.enabled

    @property
    def subagent_max_depth(self) -> int | None:
        return self.subagents.max_depth

    @property
    def subagent_max_active_children(self) -> int | None:
        return self.subagents.max_active_children

    @property
    def subagent_max_steps(self) -> int | None:
        return self.subagents.max_steps

    @property
    def subagent_max_tokens(self) -> int | None:
        return self.subagents.max_tokens

    @property
    def subagent_timeout_seconds(self) -> float | None:
        return self.subagents.timeout_seconds

    @property
    def background_tasks_enabled(self) -> bool | None:
        return self.background_tasks.enabled

    @property
    def skills_enabled(self) -> bool | None:
        return self.skills.enabled

    @property
    def mcp_enabled(self) -> bool | None:
        return self.mcp.enabled

    @property
    def mcp_servers(self) -> tuple[McpServerSettingsLayer, ...]:
        return self.mcp.servers

    @property
    def sandbox_mode(self) -> SandboxMode | None:
        return self.sandbox.mode

    @property
    def sandbox_network(self) -> SandboxNetwork | None:
        return self.sandbox.network

    @property
    def sandbox_allow_unsandboxed_commands(self) -> bool | None:
        return self.sandbox.allow_unsandboxed_commands

    def overlay(self, higher: "SettingsLayer") -> "SettingsLayer":
        return SettingsLayer(
            active_provider=higher.active_provider or self.active_provider,
            agent=AgentSettingsLayer(
                higher.max_steps if higher.max_steps is not None else self.max_steps,
                higher.max_output_tokens
                if higher.max_output_tokens is not None
                else self.max_output_tokens,
                higher.context_chars
                if higher.context_chars is not None
                else self.context_chars,
            ),
            permissions=PermissionSettingsLayer(
                higher.permission_mode or self.permission_mode,
                _union_rules(
                    self.permission_allow_rules, higher.permission_allow_rules
                ),
                _union_rules(self.permission_ask_rules, higher.permission_ask_rules),
                _union_rules(self.permission_deny_rules, higher.permission_deny_rules),
            ),
            tools=ToolSettingsLayer(
                higher.max_parallel_tool_calls
                if higher.max_parallel_tool_calls is not None
                else self.max_parallel_tool_calls,
                higher.tool_search_mode
                if higher.tool_search_mode is not None
                else self.tool_search_mode,
            ),
            subagents=SubagentSettingsLayer(
                higher.subagents_enabled
                if higher.subagents_enabled is not None
                else self.subagents_enabled,
                higher.subagent_max_depth
                if higher.subagent_max_depth is not None
                else self.subagent_max_depth,
                higher.subagent_max_active_children
                if higher.subagent_max_active_children is not None
                else self.subagent_max_active_children,
                higher.subagent_max_steps
                if higher.subagent_max_steps is not None
                else self.subagent_max_steps,
                higher.subagent_max_tokens
                if higher.subagent_max_tokens is not None
                else self.subagent_max_tokens,
                higher.subagent_timeout_seconds
                if higher.subagent_timeout_seconds is not None
                else self.subagent_timeout_seconds,
            ),
            background_tasks=BackgroundTaskSettingsLayer(
                higher.background_tasks_enabled
                if higher.background_tasks_enabled is not None
                else self.background_tasks_enabled
            ),
            skills=SkillSettingsLayer(
                higher.skills_enabled
                if higher.skills_enabled is not None
                else self.skills_enabled
            ),
            mcp=McpSettingsLayer(
                higher.mcp_enabled
                if higher.mcp_enabled is not None
                else self.mcp_enabled,
                _overlay_mcp_servers(self.mcp_servers, higher.mcp_servers),
            ),
            sandbox=SandboxSettingsLayer(
                higher.sandbox_mode or self.sandbox_mode,
                higher.sandbox_network or self.sandbox_network,
                (
                    higher.sandbox_allow_unsandboxed_commands
                    if higher.sandbox_allow_unsandboxed_commands is not None
                    else self.sandbox_allow_unsandboxed_commands
                ),
            ),
        )


class SettingsStore:
    def __init__(self, paths: MyCodePaths) -> None:
        self.paths = paths

    def load(self) -> SettingsLayer:
        merged = SettingsLayer()
        for scope in (SettingsScope.USER, SettingsScope.PROJECT, SettingsScope.LOCAL):
            merged = merged.overlay(self.load_scope(scope))
        return merged

    def load_scope(self, scope: SettingsScope) -> SettingsLayer:
        if self._project_scope_is_unavailable(scope):
            return SettingsLayer()
        path = self.paths.settings_path(scope)
        if not path.exists():
            return SettingsLayer()
        raw = self._read(path)
        return _parse_settings(raw, path=path, scope=scope)

    def write(self, scope: SettingsScope, settings: SettingsLayer) -> None:
        if self._project_scope_is_unavailable(scope):
            raise SettingsFileError(
                "Project settings are unavailable because the project config "
                f"directory is the user config directory: {self.paths.config_home}"
            )
        _validate_scope(settings, scope, self.paths.settings_path(scope))
        path = self.paths.settings_path(scope)
        existing = self._read(path) if path.exists() else {}
        if path.exists():
            _parse_settings(existing, path=path, scope=scope)
        if not isinstance(existing, dict):
            raise SettingsFileError(f"Settings root must be an object: {path}")
        _atomic_json_write(path, _merge_document(existing, settings))

    def set_user_active_provider(self, provider_id: str) -> None:
        try:
            validate_provider_id(provider_id)
        except ValueError as error:
            raise SettingsFileError(str(error)) from error
        path = self.paths.user_settings_path
        raw = self._load_editable_document(path, SettingsScope.USER)
        raw["version"] = _SCHEMA_VERSION
        raw["activeProvider"] = provider_id
        _atomic_json_write(path, raw)

    def replace_permission_rules(
        self, scope: SettingsScope, behavior: str, rules: tuple[str, ...]
    ) -> None:
        if behavior not in {"allow", "deny", "ask"}:
            raise SettingsFileError(f"Unknown permission behavior: {behavior}")
        path = self.paths.settings_path(scope)
        raw = self._load_editable_document(path, scope)
        normalized = tuple(_normalize_rule(rule, path, behavior) for rule in rules)
        permissions = _nested_object(raw, "permissions", path)
        permissions[behavior] = list(dict.fromkeys(normalized))
        raw.update(version=_SCHEMA_VERSION, permissions=permissions)
        _atomic_json_write(path, raw)

    def set_permission_mode(self, scope: SettingsScope, mode: PermissionMode) -> None:
        if scope is SettingsScope.PROJECT and mode is PermissionMode.BYPASS:
            raise SettingsFileError(
                "Shared project settings cannot enable bypassPermissions"
            )
        path = self.paths.settings_path(scope)
        raw = self._load_editable_document(path, scope)
        permissions = _nested_object(raw, "permissions", path)
        permissions["defaultMode"] = mode.value
        raw.update(version=_SCHEMA_VERSION, permissions=permissions)
        _atomic_json_write(path, raw)

    def _load_editable_document(
        self, path: Path, scope: SettingsScope
    ) -> dict[str, object]:
        if self._project_scope_is_unavailable(scope):
            raise SettingsFileError(
                "Project settings are unavailable in this workspace"
            )
        raw = self._read(path) if path.exists() else {}
        if path.exists():
            _parse_settings(raw, path=path, scope=scope)
        if not isinstance(raw, dict):
            raise SettingsFileError(f"Settings root must be an object: {path}")
        return dict(raw)

    @staticmethod
    def _read(path: Path) -> object:
        try:
            text = path.read_text(encoding="utf-8")
            return {} if not text.strip() else json.loads(text)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SettingsFileError(
                f"Cannot read settings file {path}: {error}"
            ) from error

    def _project_scope_is_unavailable(self, scope: SettingsScope) -> bool:
        return (
            scope is not SettingsScope.USER
            and self.paths.project_config_collides_with_user_storage
        )


def _parse_settings(raw: object, *, path: Path, scope: SettingsScope) -> SettingsLayer:
    if not isinstance(raw, dict):
        raise SettingsFileError(f"Settings root must be an object: {path}")
    if raw and raw.get("version") != _SCHEMA_VERSION:
        raise SettingsFileError(
            f"Settings file uses an incompatible schema: {path}. "
            "Recreate it using the v3 nested settings format."
        )
    if scope is not SettingsScope.USER:
        forbidden = sorted(
            key
            for key in (
                "activeProvider",
                "baseUrl",
                "endpoint",
                "providers",
                "credentials",
            )
            if key in raw
        )
        if forbidden:
            raise SettingsFileError(
                f"{', '.join(forbidden)} is only allowed in user storage: {path}"
            )
    agent = _nested_mapping(raw, "agent", path)
    if "model" in agent:
        raise SettingsFileError(
            "agent.model is no longer supported; move it to the selected "
            f"provider profile's defaultModel in providers.json: {path}"
        )
    if "maxTurns" in agent:
        raise SettingsFileError(
            f"agent.maxTurns is no longer supported; use agent.maxSteps: {path}"
        )
    permissions = _nested_mapping(raw, "permissions", path)
    tools = _nested_mapping(raw, "tools", path)
    subagents = _nested_mapping(raw, "subagents", path)
    background_tasks = _nested_mapping(raw, "backgroundTasks", path)
    skills = _nested_mapping(raw, "skills", path)
    mcp = _nested_mapping(raw, "mcp", path)
    sandbox = _nested_mapping(raw, "sandbox", path)
    if scope is SettingsScope.PROJECT and sandbox:
        raise SettingsFileError(
            f"sandbox is not allowed in shared project settings: {path}"
        )
    if "deferredToolThreshold" in mcp:
        raise SettingsFileError(f"Unknown setting mcp.deferredToolThreshold: {path}")
    mcp_servers = _nested_mapping(mcp, "servers", path, label="mcp.servers")
    layer = SettingsLayer(
        active_provider=_optional_string(raw, "activeProvider", path),
        agent=AgentSettingsLayer(
            _optional_positive_int(agent, "maxSteps", path, "agent.maxSteps"),
            _optional_positive_int(
                agent, "maxOutputTokens", path, "agent.maxOutputTokens"
            ),
            _optional_positive_int(agent, "contextChars", path, "agent.contextChars"),
        ),
        permissions=PermissionSettingsLayer(
            _permission_mode(permissions, path),
            _permission_rules(permissions, "allow", path),
            _permission_rules(permissions, "ask", path),
            _permission_rules(permissions, "deny", path),
        ),
        tools=ToolSettingsLayer(
            _optional_positive_int(
                tools,
                "maxParallelCalls",
                path,
                "tools.maxParallelCalls",
            ),
            _tool_search_mode(tools, path),
        ),
        subagents=SubagentSettingsLayer(
            _optional_bool(subagents, "enabled", path, "subagents.enabled"),
            _optional_positive_int(
                subagents,
                "maxDepth",
                path,
                "subagents.maxDepth",
            ),
            _optional_positive_int(
                subagents,
                "maxActiveChildren",
                path,
                "subagents.maxActiveChildren",
            ),
            _optional_positive_int(
                subagents,
                "maxSteps",
                path,
                "subagents.maxSteps",
            ),
            _optional_positive_int(
                subagents,
                "maxTokens",
                path,
                "subagents.maxTokens",
            ),
            _optional_positive_number(
                subagents,
                "timeoutSeconds",
                path,
                "subagents.timeoutSeconds",
            ),
        ),
        background_tasks=BackgroundTaskSettingsLayer(
            _optional_bool(
                background_tasks,
                "enabled",
                path,
                "backgroundTasks.enabled",
            )
        ),
        skills=SkillSettingsLayer(
            _optional_bool(skills, "enabled", path, "skills.enabled")
        ),
        mcp=McpSettingsLayer(
            _optional_bool(mcp, "enabled", path, "mcp.enabled"),
            _parse_mcp_servers(mcp_servers, path=path, scope=scope),
        ),
        sandbox=SandboxSettingsLayer(
            _enum_setting(sandbox, "mode", SandboxMode, path, "sandbox.mode"),
            _enum_setting(
                sandbox,
                "network",
                SandboxNetwork,
                path,
                "sandbox.network",
            ),
            _optional_bool(
                sandbox,
                "allowUnsandboxedCommands",
                path,
                "sandbox.allowUnsandboxedCommands",
            ),
        ),
    )
    _validate_scope(layer, scope, path)
    return layer


def _validate_scope(layer: SettingsLayer, scope: SettingsScope, path: Path) -> None:
    if layer.active_provider is not None:
        try:
            validate_provider_id(layer.active_provider)
        except ValueError as error:
            raise SettingsFileError(
                f"Invalid activeProvider in {path}: {error}"
            ) from error
    if scope is not SettingsScope.USER and layer.active_provider is not None:
        raise SettingsFileError(
            f"activeProvider is only allowed in user settings: {path}"
        )
    if (
        scope is SettingsScope.PROJECT
        and layer.permission_mode is PermissionMode.BYPASS
    ):
        raise SettingsFileError(
            f"Shared project settings cannot enable bypassPermissions: {path}"
        )
    if scope is SettingsScope.PROJECT and layer.mcp_enabled is True:
        raise SettingsFileError(
            f"Shared project settings cannot enable MCP execution: {path}"
        )
    if scope is SettingsScope.PROJECT and any(
        server.enabled for server in layer.mcp_servers
    ):
        raise SettingsFileError(
            f"Shared project MCP servers cannot be enabled directly: {path}"
        )
    if scope is SettingsScope.PROJECT and (
        layer.sandbox_mode is not None
        or layer.sandbox_network is not None
        or layer.sandbox_allow_unsandboxed_commands is not None
    ):
        raise SettingsFileError(
            f"sandbox is not allowed in shared project settings: {path}"
        )


def _settings_document(settings: SettingsLayer) -> dict[str, object]:
    document: dict[str, object] = {"version": _SCHEMA_VERSION}
    if settings.active_provider is not None:
        validate_provider_id(settings.active_provider)
        document["activeProvider"] = settings.active_provider
    agent = {
        key: value
        for key, value in (
            ("maxSteps", settings.max_steps),
            ("maxOutputTokens", settings.max_output_tokens),
            ("contextChars", settings.context_chars),
        )
        if value is not None
    }
    if agent:
        document["agent"] = agent
    permissions: dict[str, object] = {}
    if settings.permission_mode is not None:
        permissions["defaultMode"] = settings.permission_mode.value
    for name, rules in (
        ("allow", settings.permission_allow_rules),
        ("ask", settings.permission_ask_rules),
        ("deny", settings.permission_deny_rules),
    ):
        if rules:
            permissions[name] = list(
                dict.fromkeys(_normalize_rule(r, Path("<value>"), name) for r in rules)
            )
    if permissions:
        document["permissions"] = permissions
    tool_settings: dict[str, object] = {}
    if settings.max_parallel_tool_calls is not None:
        tool_settings["maxParallelCalls"] = settings.max_parallel_tool_calls
    if settings.tool_search_mode is not None:
        tool_settings["toolSearchMode"] = settings.tool_search_mode.value
    if tool_settings:
        document["tools"] = tool_settings
    subagents = {
        key: value
        for key, value in (
            ("enabled", settings.subagents_enabled),
            ("maxDepth", settings.subagent_max_depth),
            ("maxActiveChildren", settings.subagent_max_active_children),
            ("maxSteps", settings.subagent_max_steps),
            ("maxTokens", settings.subagent_max_tokens),
            ("timeoutSeconds", settings.subagent_timeout_seconds),
        )
        if value is not None
    }
    if subagents:
        document["subagents"] = subagents
    if settings.background_tasks_enabled is not None:
        document["backgroundTasks"] = {"enabled": settings.background_tasks_enabled}
    if settings.skills_enabled is not None:
        document["skills"] = {"enabled": settings.skills_enabled}
    mcp: dict[str, object] = {}
    if settings.mcp_enabled is not None:
        mcp["enabled"] = settings.mcp_enabled
    if settings.mcp_servers:
        mcp["servers"] = {
            server.name: {
                "command": server.command,
                "args": list(server.args),
                "envFrom": dict(server.env_from),
                "enabled": server.enabled,
                "startupTimeoutSeconds": server.startup_timeout_seconds,
                "callTimeoutSeconds": server.call_timeout_seconds,
            }
            for server in settings.mcp_servers
        }
    if mcp:
        document["mcp"] = mcp
    sandbox = {
        key: value.value if isinstance(value, StrEnum) else value
        for key, value in (
            ("mode", settings.sandbox_mode),
            ("network", settings.sandbox_network),
            (
                "allowUnsandboxedCommands",
                settings.sandbox_allow_unsandboxed_commands,
            ),
        )
        if value is not None
    }
    if sandbox:
        document["sandbox"] = sandbox
    return document


def _merge_document(
    existing: dict[str, object], settings: SettingsLayer
) -> dict[str, object]:
    result = dict(existing)
    incoming = _settings_document(settings)
    for key, value in incoming.items():
        if key == "mcp" and isinstance(value, dict):
            current = result.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            incoming_servers = value.get("servers")
            if isinstance(incoming_servers, dict):
                current_servers = merged.get("servers")
                servers = (
                    dict(current_servers) if isinstance(current_servers, dict) else {}
                )
                servers.update(incoming_servers)
                merged["servers"] = servers
            merged.update(
                {name: item for name, item in value.items() if name != "servers"}
            )
            result[key] = merged
        elif key in {
            "agent",
            "permissions",
            "tools",
            "subagents",
            "backgroundTasks",
            "skills",
            "sandbox",
        } and isinstance(value, dict):
            current = result.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    result["version"] = _SCHEMA_VERSION
    return result


def _nested_mapping(
    raw: dict[object, object],
    key: str,
    path: Path,
    *,
    label: str | None = None,
) -> dict[object, object]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsFileError(f"{label or key} must be an object: {path}")
    return value


def _parse_mcp_servers(
    raw: dict[object, object],
    *,
    path: Path,
    scope: SettingsScope,
) -> tuple[McpServerSettingsLayer, ...]:
    servers: list[McpServerSettingsLayer] = []
    for raw_name in sorted(raw, key=str):
        if (
            not isinstance(raw_name, str)
            or _MCP_SERVER_NAME.fullmatch(raw_name) is None
        ):
            raise SettingsFileError(
                f"MCP server name must match [a-z0-9][a-z0-9_-]{{0,63}}: {path}"
            )
        raw_server = raw[raw_name]
        if not isinstance(raw_server, dict):
            raise SettingsFileError(f"mcp.servers.{raw_name} must be an object: {path}")
        if "env" in raw_server:
            raise SettingsFileError(
                f"mcp.servers.{raw_name}.env cannot contain literal values; "
                f"use envFrom references: {path}"
            )
        command = _optional_string(
            raw_server,
            "command",
            path,
            f"mcp.servers.{raw_name}.command",
        )
        if command is None or "\x00" in command:
            raise SettingsFileError(
                f"mcp.servers.{raw_name}.command is required and cannot "
                f"contain NUL: {path}"
            )
        args = _string_array(
            raw_server,
            "args",
            path,
            f"mcp.servers.{raw_name}.args",
        )
        if any("\x00" in argument for argument in args):
            raise SettingsFileError(
                f"mcp.servers.{raw_name}.args cannot contain NUL: {path}"
            )
        enabled = _optional_bool(
            raw_server,
            "enabled",
            path,
            f"mcp.servers.{raw_name}.enabled",
        )
        servers.append(
            McpServerSettingsLayer(
                name=raw_name,
                command=command,
                args=args,
                env_from=_environment_references(
                    raw_server,
                    raw_name,
                    path,
                ),
                enabled=(scope is not SettingsScope.PROJECT)
                if enabled is None
                else enabled,
                startup_timeout_seconds=(
                    _optional_positive_number(
                        raw_server,
                        "startupTimeoutSeconds",
                        path,
                        f"mcp.servers.{raw_name}.startupTimeoutSeconds",
                    )
                    or 10.0
                ),
                call_timeout_seconds=(
                    _optional_positive_number(
                        raw_server,
                        "callTimeoutSeconds",
                        path,
                        f"mcp.servers.{raw_name}.callTimeoutSeconds",
                    )
                    or 60.0
                ),
                scope=scope,
            )
        )
    return tuple(servers)


def _string_array(
    raw: dict[object, object], key: str, path: Path, label: str
) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsFileError(f"{label} must be an array of strings: {path}")
    return tuple(value)


def _environment_references(
    raw_server: dict[object, object], server_name: str, path: Path
) -> tuple[tuple[str, str], ...]:
    raw = raw_server.get("envFrom", {})
    if not isinstance(raw, dict):
        raise SettingsFileError(
            f"mcp.servers.{server_name}.envFrom must be an object: {path}"
        )
    references: list[tuple[str, str]] = []
    for target in sorted(raw, key=str):
        source = raw[target]
        if (
            not isinstance(target, str)
            or _ENVIRONMENT_NAME.fullmatch(target) is None
            or not isinstance(source, str)
            or _ENVIRONMENT_NAME.fullmatch(source) is None
        ):
            raise SettingsFileError(
                f"mcp.servers.{server_name}.envFrom must map environment names: {path}"
            )
        references.append((target, source))
    return tuple(references)


def _overlay_mcp_servers(
    lower: tuple[McpServerSettingsLayer, ...],
    higher: tuple[McpServerSettingsLayer, ...],
) -> tuple[McpServerSettingsLayer, ...]:
    merged = {server.name: server for server in lower}
    merged.update({server.name: server for server in higher})
    return tuple(merged[name] for name in sorted(merged))


def _nested_object(raw: dict[str, object], key: str, path: Path) -> dict[str, object]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsFileError(f"{key} must be an object: {path}")
    return dict(value)


def _optional_string(
    raw: dict[object, object], key: str, path: Path, label: str | None = None
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsFileError(f"{label or key} must be a non-empty string: {path}")
    return value


def _tool_search_mode(raw: dict[object, object], path: Path) -> ToolSearchMode | None:
    value = raw.get("toolSearchMode")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsFileError(f"tools.toolSearchMode must be a string: {path}")
    try:
        return ToolSearchMode(value)
    except ValueError as error:
        raise SettingsFileError(
            f"tools.toolSearchMode must be dispatcher or native: {path}"
        ) from error


def _optional_positive_int(
    raw: dict[object, object], key: str, path: Path, label: str
) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsFileError(f"{label} must be a positive integer: {path}")
    return value


def _optional_positive_number(
    raw: dict[object, object], key: str, path: Path, label: str
) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsFileError(f"{label} must be a positive number: {path}")
    return float(value)


def _optional_bool(
    raw: dict[object, object], key: str, path: Path, label: str
) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SettingsFileError(f"{label} must be a boolean: {path}")
    return value


def _enum_setting[T: StrEnum](
    raw: dict[object, object],
    key: str,
    enum_type: type[T],
    path: Path,
    label: str,
) -> T | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsFileError(f"{label} must be a string: {path}")
    try:
        return enum_type(value)
    except ValueError as error:
        choices = ", ".join(item.value for item in enum_type)
        raise SettingsFileError(f"{label} must be one of {choices}: {path}") from error


def _permission_mode(raw: dict[object, object], path: Path) -> PermissionMode | None:
    value = raw.get("defaultMode")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsFileError(f"permissions.defaultMode must be a string: {path}")
    try:
        return PermissionMode(value)
    except ValueError as error:
        raise SettingsFileError(f"Invalid permissions.defaultMode: {path}") from error


def _permission_rules(
    raw: dict[object, object], behavior: str, path: Path
) -> tuple[str, ...]:
    value = raw.get(behavior)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SettingsFileError(
            f"permissions.{behavior} must be an array of strings: {path}"
        )
    rules: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SettingsFileError(
                f"permissions.{behavior}[{index}] must be a string: {path}"
            )
        rules.append(_normalize_rule(item, path, f"{behavior}[{index}]"))
    return tuple(dict.fromkeys(rules))


def _normalize_rule(rule: str, path: Path, label: str) -> str:
    try:
        tool, content = validate_permission_rule(rule)
    except ValueError as error:
        raise SettingsFileError(
            f"Invalid permissions.{label} in {path}: {error}"
        ) from error
    return permission_rule_to_string(tool, content)


def _union_rules(lower: tuple[str, ...], higher: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*lower, *higher)))


def _atomic_json_write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise SettingsFileError(
            f"Cannot write settings file {path}: {error}"
        ) from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


__all__ = [
    "AgentSettingsLayer",
    "BackgroundTaskSettingsLayer",
    "McpServerSettingsLayer",
    "McpSettingsLayer",
    "PermissionSettingsLayer",
    "SandboxMode",
    "SandboxNetwork",
    "SandboxSettingsLayer",
    "SettingsFileError",
    "SettingsLayer",
    "SettingsStore",
    "SkillSettingsLayer",
    "SubagentSettingsLayer",
    "ToolSettingsLayer",
]
