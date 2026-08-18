"""应用设置与项目级运行时状态的文件系统布局。"""

import hashlib
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_NON_ALPHANUMERIC = re.compile(r"[^a-zA-Z0-9]")
_MAX_SANITIZED_LENGTH = 200


class SettingsScope(StrEnum):
    """可编辑的设置作用域，具体优先级由 ``SettingsStore`` 单独管理。"""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


def sanitize_path(name: str) -> str:
    """将项目绝对路径转换为可移植的目录名。

    Claude Code 同样会替换非字母数字字符并保留 200 字符前缀。SHA-256
    为 Python 实现在不同进程和平台间提供稳定的长路径后缀。
    """

    normalized = unicodedata.normalize("NFC", name)
    sanitized = _NON_ALPHANUMERIC.sub("-", normalized)
    if len(sanitized) <= _MAX_SANITIZED_LENGTH:
        return sanitized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized[:_MAX_SANITIZED_LENGTH]}-{digest}"


@dataclass(frozen=True, slots=True)
class NanoCodePaths:
    """设置、会话记录与工具结果存储共享的已解析路径。"""

    cwd: Path
    config_home: Path

    @classmethod
    def discover(
        cls,
        cwd: Path,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "NanoCodePaths":
        """解析工作区和形如 ``~/.nano-code`` 的配置主目录。"""

        environment = os.environ if environ is None else environ
        canonical_cwd = _canonical_path(cwd)
        config_override = environment.get("NANO_CODE_CONFIG_DIR")
        if config_override:
            config_home = _canonical_path(Path(config_override).expanduser())
        else:
            user_home = Path.home() if home is None else home
            config_home = _canonical_path(user_home / ".nano-code")
        return cls(cwd=canonical_cwd, config_home=config_home)

    @property
    def projects_dir(self) -> Path:
        return self.config_home / "projects"

    @property
    def project_state_dir(self) -> Path:
        # 按规范化 cwd 分组，避免同名仓库相互干扰。
        return self.projects_dir / sanitize_path(str(self.cwd))

    @property
    def user_settings_path(self) -> Path:
        return self.config_home / "settings.json"

    @property
    def credentials_path(self) -> Path:
        """返回独立于设置文件的用户专属凭据文件。"""

        return self.config_home / ".credentials.json"

    @property
    def providers_path(self) -> Path:
        """返回用户专属且不含密钥的 provider profile 目录。"""

        return self.config_home / "providers.json"

    @property
    def model_cache_path(self) -> Path:
        return self.config_home / ".model-catalog.json"

    @property
    def project_config_dir(self) -> Path:
        return self.cwd / ".nano-code"

    @property
    def project_config_collides_with_user_storage(self) -> bool:
        """项目设置是否会占用用户配置目录。

        nano-code 在用户主目录中以默认布局启动时会发生这种情况。同一个
        文件无法同时安全表示可信用户作用域和不可信项目作用域。
        """

        return self.project_config_dir == self.config_home

    @property
    def project_settings_path(self) -> Path:
        return self.project_config_dir / "settings.json"

    @property
    def local_settings_path(self) -> Path:
        return self.project_config_dir / "settings.local.json"

    def settings_path(self, scope: SettingsScope) -> Path:
        match scope:
            case SettingsScope.USER:
                return self.user_settings_path
            case SettingsScope.PROJECT:
                return self.project_settings_path
            case SettingsScope.LOCAL:
                return self.local_settings_path

    def transcript_path(self, session_id: str) -> Path:
        return self.project_state_dir / f"{session_id}.jsonl"

    def session_dir(self, session_id: str) -> Path:
        return self.project_state_dir / session_id

    def tool_results_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "tool-results"


def _canonical_path(path: Path) -> Path:
    # ``resolve`` 会尽可能规范化符号链接，并允许尚未创建的配置路径存在。
    resolved = path.resolve(strict=False)
    return Path(unicodedata.normalize("NFC", str(resolved)))
