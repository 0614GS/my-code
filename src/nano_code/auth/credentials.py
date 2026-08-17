"""用于 API key 直接认证的精简用户级凭据存储。"""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nano_code.providers.ids import validate_provider_id

_DEFAULT_PROVIDER_ID = "anthropic"
_SCHEMA_VERSION = 2


class CredentialStoreError(ValueError):
    """凭据无法被安全读取或持久化。"""


class CredentialSource(StrEnum):
    """不暴露凭据本身的可观察凭据来源。"""

    ENVIRONMENT = "environment"
    STORED = "stored"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    api_key: str | None
    source: CredentialSource


class CredentialStore:
    """在可共享设置文件之外持久化 provider 级 API key。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_api_key(self, provider_id: str = _DEFAULT_PROVIDER_ID) -> str | None:
        _validate_id(provider_id)
        if not self.path.exists():
            return None
        keys, _legacy = self._load_keys()
        return keys.get(provider_id)

    def save_api_key(
        self, api_key: str, provider_id: str = _DEFAULT_PROVIDER_ID
    ) -> None:
        _validate_id(provider_id)
        value = api_key.strip()
        if not value:
            raise CredentialStoreError("API key must not be empty")
        if any(character.isspace() for character in value):
            raise CredentialStoreError("API key must not contain whitespace")

        keys, _legacy = self._load_keys() if self.path.exists() else ({}, False)
        keys[provider_id] = value
        self._write_keys(keys)

    def delete(self, provider_id: str = _DEFAULT_PROVIDER_ID) -> bool:
        """删除一个 provider 的 key，同时保留凭据目录。"""

        _validate_id(provider_id)
        if not self.path.exists():
            return False
        keys, legacy = self._load_keys()
        removed = keys.pop(provider_id, None) is not None
        if removed or legacy:
            self._write_keys(keys)
        return removed

    def ensure_exists(self) -> bool:
        """创建或迁移私有凭据目录。"""

        if not self.path.exists():
            self._write_keys({})
            return True
        keys, legacy = self._load_keys()
        if legacy:
            self._write_keys(keys)
        else:
            os.chmod(self.path, 0o600)
        return False

    def _load_keys(self) -> tuple[dict[str, str], bool]:
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CredentialStoreError(
                f"Cannot read credential file {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise CredentialStoreError(
                f"Credential file root must be an object: {self.path}"
            )

        if raw.get("version") != _SCHEMA_VERSION:
            raise CredentialStoreError(
                "Credential file must use schema version "
                f"{_SCHEMA_VERSION}: {self.path}. Recreate it with "
                "`nanocode auth login`."
            )
        providers = raw.get("providers")
        if not isinstance(providers, dict):
            raise CredentialStoreError(
                f"Credential providers must be an object: {self.path}"
            )
        keys: dict[str, str] = {}
        for provider_id, entry in providers.items():
            if not isinstance(provider_id, str) or not isinstance(entry, dict):
                raise CredentialStoreError(
                    f"Each provider credential must be a named object: {self.path}"
                )
            _validate_id(provider_id)
            if entry.get("kind") != "apiKey":
                raise CredentialStoreError(
                    f"Unsupported credential kind for {provider_id!r}: {self.path}"
                )
            value = _parse_api_key(entry.get("apiKey"), self.path)
            if value is not None:
                keys[provider_id] = value
        return keys, False

    def _write_keys(self, keys: Mapping[str, str]) -> None:
        existing: dict[str, object] = {}
        existing_providers: dict[object, object] = {}
        if self.path.exists():
            self._load_keys()
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = dict(loaded)
                raw_providers = loaded.get("providers")
                if isinstance(raw_providers, dict):
                    existing_providers = raw_providers
        providers: dict[str, object] = {}
        for provider_id in sorted(keys):
            _validate_id(provider_id)
            previous = existing_providers.get(provider_id)
            entry = dict(previous) if isinstance(previous, dict) else {}
            entry.update(kind="apiKey", apiKey=keys[provider_id])
            providers[provider_id] = entry
        document = dict(existing)
        document.update(version=_SCHEMA_VERSION, providers=providers)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.chmod(temporary_path, 0o600)
                json.dump(document, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        except OSError as error:
            raise CredentialStoreError(
                f"Cannot write credential file {self.path}: {error}"
            ) from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _parse_api_key(value: object, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CredentialStoreError(f"apiKey must be a non-empty string: {path}")
    return value


def _validate_id(provider_id: str) -> None:
    try:
        validate_provider_id(provider_id)
    except ValueError as error:
        raise CredentialStoreError(str(error)) from error


def resolve_api_key(
    store: CredentialStore,
    environ: Mapping[str, str] | None = None,
    *,
    provider_id: str = _DEFAULT_PROVIDER_ID,
) -> ResolvedCredential:
    """优先解析临时环境变量覆盖，其次使用持久化登录 key。"""

    environment = os.environ if environ is None else environ
    environment_key = environment.get("NANO_CODE_API_KEY") or environment.get(
        "ANTHROPIC_API_KEY"
    )
    if environment_key and environment_key.strip():
        return ResolvedCredential(environment_key, CredentialSource.ENVIRONMENT)
    stored_key = store.load_api_key(provider_id)
    if stored_key is not None:
        return ResolvedCredential(stored_key, CredentialSource.STORED)
    return ResolvedCredential(None, CredentialSource.NONE)
