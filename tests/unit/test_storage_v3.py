import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_code.auth.credentials import CredentialStore, CredentialStoreError
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.providers import ProviderProfile, ProviderProfileStore
from my_code.config.store import SettingsFileError, SettingsLayer, SettingsStore
from my_code.conversation.models import HumanMessage
from my_code.sessions._store import SessionStore
from my_code.sessions.catalog import SessionCatalog

SESSION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _paths(tmp_path: Path) -> MyCodePaths:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return MyCodePaths(workspace.resolve(), tmp_path / "config")


def test_settings_v3_preserves_unknown_nested_fields_and_rejects_v2(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "agent": {"model": "old", "futureAgent": True},
                "futureRoot": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    SettingsStore(paths).write(
        SettingsScope.USER,
        SettingsLayer(model="new"),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["agent"] == {"model": "new", "futureAgent": True}
    assert document["futureRoot"] == {"enabled": True}

    paths.user_settings_path.write_text(
        json.dumps({"version": 2, "agent": {"model": "legacy"}}), encoding="utf-8"
    )
    with pytest.raises(SettingsFileError, match=str(paths.user_settings_path)):
        SettingsStore(paths).load()


def test_provider_and_credentials_preserve_unknown_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "providers.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": 2,
                "futureRoot": 1,
                "providers": {
                    "anthropic": {
                        "protocol": "anthropic-messages",
                        "defaultModel": "old",
                        "futureProfile": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ProviderProfileStore(profile_path).write((ProviderProfile("anthropic", "new"),))
    profile_document = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile_document["futureRoot"] == 1
    assert profile_document["providers"]["anthropic"]["futureProfile"] is True

    credential_path = tmp_path / ".credentials.json"
    credential_path.write_text(
        json.dumps(
            {
                "version": 2,
                "futureRoot": 2,
                "providers": {
                    "anthropic": {
                        "kind": "apiKey",
                        "apiKey": "old",
                        "futureCredential": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    CredentialStore(credential_path).save_api_key("new")
    credential_document = json.loads(credential_path.read_text(encoding="utf-8"))
    assert credential_document["futureRoot"] == 2
    assert credential_document["providers"]["anthropic"]["futureCredential"] is True
    with pytest.raises(CredentialStoreError, match="provider ID"):
        CredentialStore(credential_path).save_api_key("secret", "../escape")


def test_session_start_metadata_catalog_and_interrupted_tail(tmp_path: Path) -> None:
    now = datetime(2026, 8, 17, 12, tzinfo=UTC)
    store = SessionStore(tmp_path, SESSION_ID, clock=lambda: now)
    message = HumanMessage("  first   prompt  ")
    store.append(message)
    store.set_title("Explicit title")

    entries = [json.loads(line) for line in store.path.read_text().splitlines()]
    assert entries[0]["type"] == "session_started"
    assert entries[1]["type"] == "human_message"
    assert entries[2]["type"] == "session_metadata"
    summary = SessionCatalog(tmp_path).list()[0]
    assert summary.title == "Explicit title"
    assert summary.last_prompt == message.content
    assert summary.provider_id == "anthropic"

    with store.path.open("ab") as handle:
        handle.write(b'{"type":')
    assert store.load().conversation == (message,)


def test_complete_bad_transcript_line_is_not_ignored(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, SESSION_ID)
    store.append(HumanMessage("hello"))
    with store.path.open("ab") as handle:
        handle.write(b'{"type":"future","schema_version":2}\n')

    with pytest.raises(ValueError, match="Invalid transcript line"):
        store.load()
