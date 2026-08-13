"""Anthropic Messages 兼容服务的具名连接 profile。"""

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nano_code.providers.validation import validate_base_url

DEFAULT_PROVIDER_ID = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"
_SCHEMA_VERSION = 1
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProviderProfileError(ValueError):
    """provider profile 目录格式错误或无法持久化。"""


class ProviderProtocol(StrEnum):
    """nano-code 支持的线路协议。"""

    ANTHROPIC_MESSAGES = "anthropic-messages"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """构造模型 provider 适配器所需的非敏感设置。"""

    id: str
    model: str
    protocol: ProviderProtocol = ProviderProtocol.ANTHROPIC_MESSAGES
    base_url: str | None = None

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.id) is None:
            raise ProviderProfileError(
                "provider ID must match [a-z0-9][a-z0-9_-]{0,63}"
            )
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
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            raise ProviderProfileError(
                "Provider profiles must use schema version "
                f"{_SCHEMA_VERSION}: {self.path}"
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
            result[provider_id] = _parse_profile(provider_id, value, self.path)
        return result

    def ensure_exists(self, default: ProviderProfile) -> bool:
        """创建包含 ``default`` 的目录，并返回它是否为新建。"""

        if self.path.exists():
            self.load()
            return False
        self.write((default,))
        return True

    def write(self, profiles: Iterable[ProviderProfile]) -> None:
        indexed = {profile.id: profile for profile in profiles}
        document = {
            "version": _SCHEMA_VERSION,
            "providers": {
                provider_id: _profile_document(indexed[provider_id])
                for provider_id in sorted(indexed)
            },
        }
        _atomic_private_json_write(self.path, document)


def _parse_profile(
    provider_id: str, raw: dict[object, object], path: Path
) -> ProviderProfile:
    model = raw.get("model")
    protocol = raw.get("protocol")
    base_url = raw.get("baseUrl")
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
    return ProviderProfile(
        id=provider_id,
        model=model,
        protocol=parsed_protocol,
        base_url=base_url,
    )


def _profile_document(profile: ProviderProfile) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": profile.protocol.value,
        "model": profile.model,
    }
    if profile.base_url is not None:
        document["baseUrl"] = profile.base_url
    return document


def _atomic_private_json_write(path: Path, document: object) -> None:
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
