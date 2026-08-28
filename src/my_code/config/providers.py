"""按协议配置的具名 provider profile。"""

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_code.config.validation import validate_base_url
from my_code.model.capabilities import ModelLimits
from my_code.model.primitives import validate_provider_id

_SCHEMA_VERSION = 3
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
OPENAI_API_BASE_URL = "https://api.openai.com/v1"


class ProviderProfileError(ValueError):
    """provider profile 目录格式错误或无法持久化。"""


class ProviderProtocol(StrEnum):
    """my-code 支持的线路协议。"""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_RESPONSES = "openai-responses"


@dataclass(frozen=True, slots=True)
class ReasoningConfig:
    enabled: bool = True
    effort: str = "auto"
    context: str = "auto"

    def for_protocol(self, protocol: ProviderProtocol) -> "ReasoningConfig":
        anthropic = {"auto", "low", "medium", "high", "max"}
        openai = {"auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
        allowed = (
            anthropic if protocol is ProviderProtocol.ANTHROPIC_MESSAGES else openai
        )
        if self.effort not in allowed:
            raise ProviderProfileError(
                f"Unsupported {protocol.value} reasoning effort: {self.effort}"
            )
        if self.context not in {"auto", "current_turn", "all_turns"}:
            raise ProviderProfileError(f"Unsupported reasoning context: {self.context}")
        if protocol is ProviderProtocol.ANTHROPIC_MESSAGES and self.context != "auto":
            raise ProviderProfileError("Anthropic reasoning context must be auto")
        return self


@dataclass(frozen=True, slots=True)
class CompactConfig:
    trigger_input_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.trigger_input_tokens is not None and self.trigger_input_tokens < 1:
            raise ProviderProfileError("compact triggerInputTokens must be positive")


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """构造模型 provider 适配器所需的非敏感设置。"""

    id: str
    protocol: ProviderProtocol
    model: str
    base_url: str | None = None
    reasoning: ReasoningConfig = ReasoningConfig()
    limits: ModelLimits = ModelLimits()
    compact: CompactConfig = CompactConfig()

    def __post_init__(self) -> None:
        try:
            validate_provider_id(self.id)
        except ValueError as error:
            raise ProviderProfileError(str(error)) from error
        if not self.model.strip():
            raise ProviderProfileError("provider model must be a non-empty string")
        if self.base_url is not None:
            try:
                normalized = validate_base_url(self.base_url)
            except ValueError as error:
                raise ProviderProfileError(
                    f"invalid provider base URL: {error}"
                ) from error
            object.__setattr__(self, "base_url", normalized)
        self.reasoning.for_protocol(self.protocol)
        known_limit = self.limits.effective_input_limit(
            self.limits.max_output_tokens or 1
        )
        if (
            known_limit is not None
            and self.compact.trigger_input_tokens is not None
            and self.compact.trigger_input_tokens > known_limit
        ):
            raise ProviderProfileError(
                "compact triggerInputTokens exceeds the profile model input limit"
            )


class ProviderProfileStore:
    """原子持久化用户所有且不含凭据的 provider profile。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, ProviderProfile]:
        if not self.path.exists():
            return {}
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderProfileError(
                f"Cannot read provider profiles {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict) or raw.get("version") not in {
            2,
            _SCHEMA_VERSION,
        }:
            raise ProviderProfileError(
                "Provider profiles must use schema version "
                f"{_SCHEMA_VERSION}: {self.path}. Recreate the provider profile."
            )
        providers = raw.get("providers")
        if not isinstance(providers, dict):
            raise ProviderProfileError(f"providers must be an object: {self.path}")

        result: dict[str, ProviderProfile] = {}
        for provider_id, value in providers.items():
            if not isinstance(provider_id, str) or not isinstance(value, dict):
                raise ProviderProfileError(
                    f"Each provider must be a named object: {self.path}"
                )
            result[provider_id] = _parse_profile(
                provider_id,
                value,
                self.path,
                legacy=raw.get("version") == 2,
                token_schema=raw.get("version") == _SCHEMA_VERSION,
            )
        return result

    def ensure_empty_exists(self) -> bool:
        """Create an empty catalog without inventing a provider profile."""

        if self.path.exists():
            self.load()
            return False
        self.write(())
        return True

    def write(self, profiles: Iterable[ProviderProfile]) -> None:
        indexed = {profile.id: profile for profile in profiles}
        existing: dict[str, object] = {}
        existing_providers: dict[object, object] = {}
        if self.path.exists():
            self.load()
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = dict(loaded)
                raw_providers = loaded.get("providers")
                if isinstance(raw_providers, dict):
                    existing_providers = raw_providers
        providers: dict[str, object] = {}
        for provider_id in sorted(indexed):
            previous = existing_providers.get(provider_id)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry.update(_profile_document(indexed[provider_id]))
            providers[provider_id] = entry
        document = dict(existing)
        document.update(version=_SCHEMA_VERSION, providers=providers)
        atomic_private_json_write(self.path, document)


def _parse_profile(
    provider_id: str,
    raw: dict[object, object],
    path: Path,
    *,
    legacy: bool = False,
    token_schema: bool = False,
) -> ProviderProfile:
    model = raw.get("defaultModel")
    protocol = raw.get("protocol")
    base_url = raw.get("baseUrl")
    reasoning_raw = raw.get("reasoning")
    if not isinstance(model, str):
        raise ProviderProfileError(f"provider model must be a string: {path}")
    if not isinstance(protocol, str):
        raise ProviderProfileError(f"provider protocol must be a string: {path}")
    if base_url is not None and not isinstance(base_url, str):
        raise ProviderProfileError(f"provider baseUrl must be a string: {path}")
    try:
        parsed_protocol = ProviderProtocol(protocol)
    except ValueError as error:
        raise ProviderProfileError(
            f"Unsupported provider protocol {protocol!r}: {path}"
        ) from error
    if legacy:
        reasoning = ReasoningConfig(enabled=False)
    else:
        if reasoning_raw is None:
            reasoning = ReasoningConfig()
        else:
            if not isinstance(reasoning_raw, dict):
                raise ProviderProfileError(
                    f"provider reasoning must be an object: {path}"
                )
            enabled = reasoning_raw.get("enabled", True)
            effort = reasoning_raw.get("effort", "auto")
            context = reasoning_raw.get("context", "auto")
            if (
                not isinstance(enabled, bool)
                or not isinstance(effort, str)
                or not isinstance(context, str)
            ):
                raise ProviderProfileError(f"invalid provider reasoning config: {path}")
            reasoning = ReasoningConfig(enabled, effort, context)
    limits = ModelLimits()
    compact = CompactConfig()
    if token_schema:
        limits = _parse_limits(raw.get("limits"), path)
        compact = _parse_compact(raw.get("compact"), path)
    return ProviderProfile(
        id=provider_id,
        model=model,
        protocol=parsed_protocol,
        base_url=base_url,
        reasoning=reasoning,
        limits=limits,
        compact=compact,
    )


def _profile_document(profile: ProviderProfile) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": profile.protocol.value,
        "defaultModel": profile.model,
        "reasoning": {
            "enabled": profile.reasoning.enabled,
            "effort": profile.reasoning.effort,
            "context": profile.reasoning.context,
        },
        "limits": {
            "contextWindowTokens": profile.limits.context_window_tokens,
            "maxInputTokens": profile.limits.max_input_tokens,
            "maxOutputTokens": profile.limits.max_output_tokens,
        },
        "compact": {
            "triggerInputTokens": profile.compact.trigger_input_tokens,
        },
    }
    if profile.base_url is not None:
        document["baseUrl"] = profile.base_url
    return document


def _parse_limits(value: object, path: Path) -> ModelLimits:
    if value is None:
        return ModelLimits()
    if not isinstance(value, dict):
        raise ProviderProfileError(f"provider limits must be an object: {path}")
    return ModelLimits(
        _optional_positive(value, "contextWindowTokens", path),
        _optional_positive(value, "maxInputTokens", path),
        _optional_positive(value, "maxOutputTokens", path),
    )


def _parse_compact(value: object, path: Path) -> CompactConfig:
    if value is None:
        return CompactConfig()
    if not isinstance(value, dict):
        raise ProviderProfileError(f"provider compact must be an object: {path}")
    return CompactConfig(_optional_positive(value, "triggerInputTokens", path))


def _optional_positive(raw: dict[object, object], key: str, path: Path) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProviderProfileError(f"provider {key} must be positive or null: {path}")
    return value


def atomic_private_json_write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
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
        raise ProviderProfileError(
            f"Cannot write provider profiles {path}: {error}"
        ) from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "ANTHROPIC_API_BASE_URL",
    "CompactConfig",
    "ProviderProfile",
    "ProviderProfileError",
    "ProviderProfileStore",
    "ProviderProtocol",
    "OPENAI_API_BASE_URL",
    "ReasoningConfig",
    "atomic_private_json_write",
]
