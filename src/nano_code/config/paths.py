"""Filesystem layout for settings and per-project runtime state."""

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
    """Editable settings scopes, ordered separately by ``SettingsStore``."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


def sanitize_path(name: str) -> str:
    """Convert an absolute project path into a portable directory name.

    Claude Code uses the same non-alphanumeric replacement and 200-character
    prefix. SHA-256 gives the Python implementation a stable long-path suffix
    across processes and platforms.
    """

    normalized = unicodedata.normalize("NFC", name)
    sanitized = _NON_ALPHANUMERIC.sub("-", normalized)
    if len(sanitized) <= _MAX_SANITIZED_LENGTH:
        return sanitized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized[:_MAX_SANITIZED_LENGTH]}-{digest}"


@dataclass(frozen=True, slots=True)
class NanoCodePaths:
    """Resolved paths shared by settings, transcripts, and tool-result storage."""

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
        """Resolve the workspace and the ``~/.nano-code``-style config home."""

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
        # Grouping by canonical cwd keeps similarly named repositories isolated.
        return self.projects_dir / sanitize_path(str(self.cwd))

    @property
    def user_settings_path(self) -> Path:
        return self.config_home / "settings.json"

    @property
    def credentials_path(self) -> Path:
        """Return the user-only credential file, separate from settings."""

        return self.config_home / ".credentials.json"

    @property
    def providers_path(self) -> Path:
        """Return the user-only, non-secret provider profile catalog."""

        return self.config_home / "providers.json"

    @property
    def project_config_dir(self) -> Path:
        return self.cwd / ".nano-code"

    @property
    def project_config_collides_with_user_storage(self) -> bool:
        """Whether project settings would occupy the user config directory.

        This occurs with the default layout when nano-code starts in the user's
        home directory. One file cannot safely represent both a trusted user
        scope and an untrusted project scope.
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
    # ``resolve`` canonicalizes symlinks when possible and is non-strict for
    # config paths that have not been materialized yet.
    resolved = path.resolve(strict=False)
    return Path(unicodedata.normalize("NFC", str(resolved)))
