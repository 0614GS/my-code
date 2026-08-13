"""完整解析后的运行时配置。"""

from dataclasses import dataclass
from pathlib import Path

from nano_code.auth import CredentialSource
from nano_code.config.paths import NanoCodePaths
from nano_code.permissions import PermissionMode
from nano_code.providers.validation import validate_base_url


@dataclass(frozen=True, slots=True)
class Settings:
    paths: NanoCodePaths
    provider_id: str
    model: str
    permission_mode: PermissionMode
    max_turns: int
    max_output_tokens: int
    context_chars: int
    interactive: bool
    api_key: str | None = None
    credential_source: CredentialSource = CredentialSource.NONE
    base_url: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        if not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if self.base_url is not None:
            object.__setattr__(self, "base_url", validate_base_url(self.base_url))
        for name, value in (
            ("max_turns", self.max_turns),
            ("max_output_tokens", self.max_output_tokens),
            ("context_chars", self.context_chars),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def cwd(self) -> Path:
        """暴露规范化工作区，不重复维护路径状态。"""

        return self.paths.cwd
