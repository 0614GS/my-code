"""跨入口共享的 Agent settings 解析与完整运行时快照。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from my_code.auth.credentials import (
    CredentialSource,
    CredentialStore,
    resolve_api_key,
)
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.providers import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    CompactConfig,
    ProviderProfile,
    ProviderProfileStore,
    ProviderProtocol,
    ReasoningConfig,
)
from my_code.config.store import SettingsLayer, SettingsStore
from my_code.config.validation import validate_base_url
from my_code.model.capabilities import ModelDescriptor, ModelLimits
from my_code.model.primitives import validate_provider_id
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
)
from my_code.permissions.rules import validate_permission_rule

DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_CONTEXT_CHARS = 160_000


@dataclass(frozen=True, slots=True)
class SettingsOverrides:
    """一个 driving adapter 可显式覆盖的 settings。"""

    provider_id: str | None = None
    model: str | None = None
    base_url: str | None = None
    permission_mode: PermissionMode | None = None
    max_steps: int | None = None
    max_output_tokens: int | None = None
    context_chars: int | None = None


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """完整解析且可用于组装一次 Agent 生命周期的配置快照。"""

    paths: MyCodePaths
    provider_id: str
    model: str
    permission_mode: PermissionMode
    max_steps: int | None
    max_output_tokens: int
    context_chars: int
    interactive: bool
    permission_rules: tuple[PermissionRule, ...] = ()
    api_key: str | None = None
    credential_source: CredentialSource = CredentialSource.NONE
    base_url: str | None = None
    protocol: ProviderProtocol = ProviderProtocol.ANTHROPIC_MESSAGES
    reasoning: ReasoningConfig = ReasoningConfig()
    model_limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()
    model_descriptor: ModelDescriptor | None = None
    model_discovered_at: str | None = None
    model_discovery_error: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        if not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if self.base_url is not None:
            object.__setattr__(self, "base_url", validate_base_url(self.base_url))
        if self.max_steps is not None and self.max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        for name, value in (
            ("max_output_tokens", self.max_output_tokens),
            ("context_chars", self.context_chars),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def cwd(self) -> Path:
        return self.paths.cwd


class SettingsResolver:
    """集中解析文件、环境、provider、凭据和入口覆盖。"""

    def __init__(
        self,
        paths: MyCodePaths,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.environ = os.environ if environ is None else environ
        self.store = SettingsStore(paths)

    @classmethod
    def for_workspace(
        cls,
        cwd: Path,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "SettingsResolver":
        workspace = cwd.resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        return cls(
            MyCodePaths.discover(workspace, environ=environ, home=home),
            environ=environ,
        )

    def active_provider_id(self, override: str | None = None) -> str:
        user = self.store.load_scope(SettingsScope.USER)
        provider_id = (
            override
            or self.environ.get("MY_CODE_PROVIDER")
            or user.active_provider
            or DEFAULT_PROVIDER_ID
        )
        validate_provider_id(provider_id)
        return provider_id

    def resolve(
        self,
        overrides: SettingsOverrides | None = None,
        *,
        interactive: bool,
    ) -> AgentSettings:
        actual_overrides = overrides or SettingsOverrides()
        stored = self.store.load()
        user = self.store.load_scope(SettingsScope.USER)
        local = self.store.load_scope(SettingsScope.LOCAL)
        project = self.store.load_scope(SettingsScope.PROJECT).overlay(local)
        provider_id = self.active_provider_id(actual_overrides.provider_id)
        profiles = ProviderProfileStore(self.paths.providers_path).load()
        if not profiles:
            profiles = {
                DEFAULT_PROVIDER_ID: ProviderProfile(
                    id=DEFAULT_PROVIDER_ID,
                    model=DEFAULT_MODEL,
                )
            }
        try:
            profile = profiles[provider_id]
        except KeyError as error:
            choices = ", ".join(sorted(profiles)) or "<none>"
            raise ValueError(
                f"Unknown provider {provider_id!r}; configured providers: {choices}"
            ) from error

        credential = resolve_api_key(
            CredentialStore(self.paths.credentials_path),
            self.environ,
            provider_id=provider_id,
            protocol=profile.protocol.value,
        )
        prefix = (
            "OPENAI"
            if profile.protocol is ProviderProtocol.OPENAI_RESPONSES
            else "ANTHROPIC"
        )
        model = (
            actual_overrides.model
            or self.environ.get(f"{prefix}_MODEL")
            or stored.model
            or profile.model
        )
        base_url = (
            actual_overrides.base_url
            or self.environ.get(f"{prefix}_BASE_URL")
            or profile.base_url
        )
        return AgentSettings(
            paths=self.paths,
            provider_id=provider_id,
            model=model,
            permission_mode=(
                actual_overrides.permission_mode
                if actual_overrides.permission_mode is not None
                else stored.permission_mode or PermissionMode.DEFAULT
            ),
            max_steps=(
                actual_overrides.max_steps
                if actual_overrides.max_steps is not None
                else stored.max_steps
            ),
            max_output_tokens=(
                actual_overrides.max_output_tokens
                if actual_overrides.max_output_tokens is not None
                else stored.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
            ),
            context_chars=(
                actual_overrides.context_chars
                if actual_overrides.context_chars is not None
                else stored.context_chars or DEFAULT_CONTEXT_CHARS
            ),
            interactive=interactive,
            permission_rules=_resolve_permission_rules(user, project, local),
            api_key=credential.api_key,
            credential_source=credential.source,
            base_url=base_url,
            protocol=profile.protocol,
            reasoning=profile.reasoning,
            model_limits=profile.limits,
            compact=profile.compact,
        )


def _resolve_permission_rules(
    user: SettingsLayer,
    project: SettingsLayer,
    local: SettingsLayer,
) -> tuple[PermissionRule, ...]:
    """按 local > project > user 合并规则，重复规则保留最高优先级来源。"""

    behaviors = {
        "allow": PermissionBehavior.ALLOW,
        "deny": PermissionBehavior.DENY,
        "ask": PermissionBehavior.ASK,
    }
    merged: list[PermissionRule] = []
    seen: set[tuple[str, str, str | None]] = set()
    for layer, source in (
        (local, "localSettings"),
        (project, "projectSettings"),
        (user, "userSettings"),
    ):
        for behavior_name, behavior in behaviors.items():
            for rule_string in getattr(layer, f"permission_{behavior_name}_rules"):
                tool_name, content = validate_permission_rule(rule_string)
                key = (tool_name, behavior.value, content)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(
                    PermissionRule(
                        tool_name,
                        behavior,
                        content,
                        source=source,
                    )
                )
    return tuple(merged)


__all__ = [
    "AgentSettings",
    "SettingsOverrides",
    "SettingsResolver",
]
