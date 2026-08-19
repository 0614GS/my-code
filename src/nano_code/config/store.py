"""Layered v3 JSON settings with strict known-field validation."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.model.primitives import validate_provider_id
from nano_code.permissions.models import PermissionMode
from nano_code.permissions.rules import (
    permission_rule_to_string,
    validate_permission_rule,
)

_SCHEMA_VERSION = 3


class SettingsFileError(ValueError):
    """A settings file exists but cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class AgentSettingsLayer:
    model: str | None = None
    max_steps: int | None = None
    max_output_tokens: int | None = None
    context_chars: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionSettingsLayer:
    default_mode: PermissionMode | None = None
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, init=False)
class SettingsLayer:
    """A partial settings layer; nested values mirror the on-disk domains."""

    active_provider: str | None
    agent: AgentSettingsLayer
    permissions: PermissionSettingsLayer

    def __init__(
        self,
        *,
        active_provider: str | None = None,
        agent: AgentSettingsLayer | None = None,
        permissions: PermissionSettingsLayer | None = None,
        # Convenience aliases for callers while the disk schema remains nested.
        model: str | None = None,
        permission_mode: PermissionMode | None = None,
        permission_allow_rules: tuple[str, ...] = (),
        permission_deny_rules: tuple[str, ...] = (),
        permission_ask_rules: tuple[str, ...] = (),
        max_steps: int | None = None,
        max_output_tokens: int | None = None,
        context_chars: int | None = None,
    ) -> None:
        if agent is not None and any(
            value is not None
            for value in (model, max_steps, max_output_tokens, context_chars)
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
        object.__setattr__(self, "active_provider", active_provider)
        object.__setattr__(
            self,
            "agent",
            agent
            or AgentSettingsLayer(model, max_steps, max_output_tokens, context_chars),
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

    @property
    def model(self) -> str | None:
        return self.agent.model

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

    def overlay(self, higher: "SettingsLayer") -> "SettingsLayer":
        return SettingsLayer(
            active_provider=higher.active_provider or self.active_provider,
            agent=AgentSettingsLayer(
                higher.model if higher.model is not None else self.model,
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
        )


class SettingsStore:
    def __init__(self, paths: NanoCodePaths) -> None:
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
    if "maxTurns" in agent:
        raise SettingsFileError(
            f"agent.maxTurns is no longer supported; use agent.maxSteps: {path}"
        )
    permissions = _nested_mapping(raw, "permissions", path)
    layer = SettingsLayer(
        active_provider=_optional_string(raw, "activeProvider", path),
        agent=AgentSettingsLayer(
            _optional_string(agent, "model", path, "agent.model"),
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


def _settings_document(settings: SettingsLayer) -> dict[str, object]:
    document: dict[str, object] = {"version": _SCHEMA_VERSION}
    if settings.active_provider is not None:
        validate_provider_id(settings.active_provider)
        document["activeProvider"] = settings.active_provider
    agent = {
        key: value
        for key, value in (
            ("model", settings.model),
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
    return document


def _merge_document(
    existing: dict[str, object], settings: SettingsLayer
) -> dict[str, object]:
    result = dict(existing)
    incoming = _settings_document(settings)
    for key, value in incoming.items():
        if key in {"agent", "permissions"} and isinstance(value, dict):
            current = result.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    result["version"] = _SCHEMA_VERSION
    return result


def _nested_mapping(
    raw: dict[object, object], key: str, path: Path
) -> dict[object, object]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsFileError(f"{key} must be an object: {path}")
    return value


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


def _optional_positive_int(
    raw: dict[object, object], key: str, path: Path, label: str
) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsFileError(f"{label} must be a positive integer: {path}")
    return value


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
    "PermissionSettingsLayer",
    "SettingsFileError",
    "SettingsLayer",
    "SettingsStore",
]
