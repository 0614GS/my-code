import json
import stat
from pathlib import Path

import pytest

from my_code.auth.credentials import (
    CredentialSource,
    CredentialStore,
    CredentialStoreError,
    resolve_api_key,
)


def test_credential_store_round_trip_uses_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "config" / ".credentials.json"
    store = CredentialStore(path)

    store.save_api_key("sk-test-value", "anthropic")

    assert store.load_api_key("anthropic") == "sk-test-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "providers": {"anthropic": {"kind": "apiKey", "apiKey": "sk-test-value"}},
    }


def test_environment_cannot_override_stored_key(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / ".credentials.json")
    store.save_api_key("stored", "anthropic")

    resolved = resolve_api_key(store, provider_id="anthropic")

    assert resolved.api_key == "stored"
    assert resolved.source is CredentialSource.STORED


def test_delete_is_idempotent(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / ".credentials.json")
    store.save_api_key("stored", "anthropic")

    assert store.delete("anthropic") is True
    assert store.delete("anthropic") is False
    assert store.path.exists()
    assert store.load_api_key("anthropic") is None


def test_credentials_are_scoped_by_provider(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / ".credentials.json")
    store.save_api_key("first-key", "first")
    store.save_api_key("second-key", "second")

    assert store.load_api_key("first") == "first-key"
    assert store.load_api_key("second") == "second-key"
    assert store.load_api_key("missing") is None


def test_malformed_credential_file_fails_without_exposing_contents(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text('{"anthropicApiKey": 3}', encoding="utf-8")

    with pytest.raises(CredentialStoreError, match="schema version 2"):
        CredentialStore(path).load_api_key("anthropic")
