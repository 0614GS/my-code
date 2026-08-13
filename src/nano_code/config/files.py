"""分层 JSON 设置加载与原子持久化。"""

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.permissions import PermissionMode
from nano_code.providers.validation import validate_base_url


class SettingsFileError(ValueError):
    """设置文件存在，但无法被安全解析或校验。"""


@dataclass(frozen=True, slots=True)
class StoredSettings:
    """MVP 支持的设置；``None`` 表示本层未提供该值。"""

    model: str | None = None
    base_url: str | None = None
    active_provider: str | None = None
    permission_mode: PermissionMode | None = None
    max_turns: int | None = None
    max_output_tokens: int | None = None
    context_chars: int | None = None

    def overlay(self, higher: "StoredSettings") -> "StoredSettings":
        """返回应用了所有显式高优先级值后的当前层。"""

        return replace(
            self,
            model=higher.model if higher.model is not None else self.model,
            base_url=(
                higher.base_url if higher.base_url is not None else self.base_url
            ),
            active_provider=(
                higher.active_provider
                if higher.active_provider is not None
                else self.active_provider
            ),
            permission_mode=(
                higher.permission_mode
                if higher.permission_mode is not None
                else self.permission_mode
            ),
            max_turns=(
                higher.max_turns if higher.max_turns is not None else self.max_turns
            ),
            max_output_tokens=(
                higher.max_output_tokens
                if higher.max_output_tokens is not None
                else self.max_output_tokens
            ),
            context_chars=(
                higher.context_chars
                if higher.context_chars is not None
                else self.context_chars
            ),
        )


class SettingsStore:
    """按优先级读取用户、项目共享和项目本地设置。"""

    def __init__(self, paths: NanoCodePaths) -> None:
        self.paths = paths

    def load(self) -> StoredSettings:
        """按照用户 < 项目 < 本地的顺序合并设置，与 Claude Code 保持一致。"""

        merged = StoredSettings()
        for scope in (
            SettingsScope.USER,
            SettingsScope.PROJECT,
            SettingsScope.LOCAL,
        ):
            merged = merged.overlay(self.load_scope(scope))
        return merged

    def load_scope(self, scope: SettingsScope) -> StoredSettings:
        if self._project_scope_is_unavailable(scope):
            # 从 $HOME 启动时，~/.nano-code 既像全局存储又像项目目录。
            # 用户数据绝不能按可信度更低的项目校验规则重新解释。
            return StoredSettings()
        path = self.paths.settings_path(scope)
        if not path.exists():
            return StoredSettings()
        try:
            contents = path.read_text(encoding="utf-8")
            raw: object = {} if not contents.strip() else json.loads(contents)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SettingsFileError(
                f"Cannot read settings file {path}: {error}"
            ) from error
        return _parse_settings(raw, path=path, scope=scope)

    def write(self, scope: SettingsScope, settings: StoredSettings) -> None:
        """以仅属主可访问的权限原子替换一个设置文件。"""

        if self._project_scope_is_unavailable(scope):
            raise SettingsFileError(
                "Project settings are unavailable because the project config "
                f"directory is the user config directory: {self.paths.config_home}"
            )
        if scope is not SettingsScope.USER and settings.base_url is not None:
            raise SettingsFileError("baseUrl is only allowed in user settings")
        if scope is not SettingsScope.USER and settings.active_provider is not None:
            raise SettingsFileError("activeProvider is only allowed in user settings")
        if (
            scope is SettingsScope.PROJECT
            and settings.permission_mode is PermissionMode.BYPASS
        ):
            raise SettingsFileError(
                "Shared project settings cannot enable bypassPermissions"
            )

        path = self.paths.settings_path(scope)
        document = _settings_document(settings)
        _atomic_json_write(path, document)

    def set_user_active_provider(self, provider_id: str) -> None:
        """将当前 provider 合并进用户设置，同时保留未知字段。"""

        if not provider_id.strip():
            raise SettingsFileError("activeProvider must be a non-empty string")
        path = self.paths.user_settings_path
        raw: object = {}
        if path.exists():
            try:
                contents = path.read_text(encoding="utf-8")
                raw = {} if not contents.strip() else json.loads(contents)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SettingsFileError(
                    f"Cannot read settings file {path}: {error}"
                ) from error
        _parse_settings(raw, path=path, scope=SettingsScope.USER)
        if not isinstance(raw, dict):
            raise SettingsFileError(f"Settings root must be an object: {path}")
        document = dict(raw)
        document["version"] = 1
        document["activeProvider"] = provider_id
        _atomic_json_write(path, document)

    def _project_scope_is_unavailable(self, scope: SettingsScope) -> bool:
        return (
            scope is not SettingsScope.USER
            and self.paths.project_config_collides_with_user_storage
        )


def _atomic_json_write(path: Path, document: object) -> None:
    """以仅属主可访问的权限原子写入一个 JSON 文档。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise SettingsFileError(
            f"Cannot write settings file {path}: {error}"
        ) from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _parse_settings(
    raw: object,
    *,
    path: Path,
    scope: SettingsScope,
) -> StoredSettings:
    if not isinstance(raw, dict):
        raise SettingsFileError(f"Settings root must be an object: {path}")

    model = _optional_non_empty_string(raw, "model", path)
    base_url = _optional_base_url(raw, path)
    active_provider = _optional_non_empty_string(raw, "activeProvider", path)
    if scope is not SettingsScope.USER and base_url is not None:
        # 仓库控制的 endpoint 可能窃取用户 API key。在支持工作区信任前，
        # provider 路由只能配置在用户作用域。
        raise SettingsFileError(f"baseUrl is only allowed in user settings: {path}")
    if scope is not SettingsScope.USER and active_provider is not None:
        raise SettingsFileError(
            f"activeProvider is only allowed in user settings: {path}"
        )
    permission_mode = _parse_permission_mode(raw, path)
    if scope is SettingsScope.PROJECT and permission_mode is PermissionMode.BYPASS:
        # MVP 尚无工作区信任流程，因此仅打开仓库时，仓库内文件不得关闭权限边界。
        raise SettingsFileError(
            f"Shared project settings cannot enable bypassPermissions: {path}"
        )
    return StoredSettings(
        model=model,
        base_url=base_url,
        active_provider=active_provider,
        permission_mode=permission_mode,
        max_turns=_optional_positive_int(raw, "maxTurns", path),
        max_output_tokens=_optional_positive_int(raw, "maxOutputTokens", path),
        context_chars=_optional_positive_int(raw, "contextChars", path),
    )


def _optional_non_empty_string(
    raw: dict[object, object], key: str, path: Path
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsFileError(f"{key} must be a non-empty string: {path}")
    return value


def _optional_positive_int(
    raw: dict[object, object], key: str, path: Path
) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsFileError(f"{key} must be a positive integer: {path}")
    return value


def _optional_base_url(raw: dict[object, object], path: Path) -> str | None:
    value = _optional_non_empty_string(raw, "baseUrl", path)
    if value is None:
        return None
    try:
        return validate_base_url(value)
    except ValueError as error:
        raise SettingsFileError(f"Invalid baseUrl in {path}: {error}") from error


def _parse_permission_mode(
    raw: dict[object, object], path: Path
) -> PermissionMode | None:
    permissions = raw.get("permissions")
    if permissions is None:
        return None
    if not isinstance(permissions, dict):
        raise SettingsFileError(f"permissions must be an object: {path}")
    value = permissions.get("defaultMode")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsFileError(f"permissions.defaultMode must be a string: {path}")
    try:
        return PermissionMode(value)
    except ValueError as error:
        choices = ", ".join(mode.value for mode in PermissionMode)
        raise SettingsFileError(
            f"permissions.defaultMode must be one of {choices}: {path}"
        ) from error


def _settings_document(settings: StoredSettings) -> dict[str, object]:
    document: dict[str, object] = {"version": 1}
    if settings.model is not None:
        if not settings.model.strip():
            raise SettingsFileError("model must be a non-empty string")
        document["model"] = settings.model
    if settings.base_url is not None:
        try:
            document["baseUrl"] = validate_base_url(settings.base_url)
        except ValueError as error:
            raise SettingsFileError(f"Invalid baseUrl: {error}") from error
    if settings.active_provider is not None:
        if not settings.active_provider.strip():
            raise SettingsFileError("activeProvider must be a non-empty string")
        document["activeProvider"] = settings.active_provider
    if settings.permission_mode is not None:
        document["permissions"] = {"defaultMode": settings.permission_mode.value}
    for key, value in (
        ("maxTurns", settings.max_turns),
        ("maxOutputTokens", settings.max_output_tokens),
        ("contextChars", settings.context_chars),
    ):
        if value is not None:
            if value <= 0:
                raise SettingsFileError(f"{key} must be a positive integer")
            document[key] = value
    return document
