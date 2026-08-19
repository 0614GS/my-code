import json
import stat
from pathlib import Path

import pytest

from my_code.config.providers import (
    CompactConfig,
    ProviderProfile,
    ProviderProfileError,
    ProviderProfileStore,
    ProviderProtocol,
    ReasoningConfig,
)
from my_code.model.capabilities import ModelLimits


def test_provider_profiles_round_trip_without_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config" / "providers.json"
    store = ProviderProfileStore(path)
    profile = ProviderProfile(
        id="company-gateway",
        protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
        base_url="https://gateway.example/anthropic",
        model="compatible-model",
    )

    store.write((profile,))

    assert store.load() == {profile.id: profile}
    assert "apiKey" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_provider_catalog_requires_supported_protocol(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {
                    "custom": {"protocol": "openai", "defaultModel": "model"}
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderProfileError, match="Unsupported provider protocol"):
        ProviderProfileStore(path).load()


def test_provider_id_is_safe_for_configuration_keys() -> None:
    with pytest.raises(ProviderProfileError, match="provider ID"):
        ProviderProfile(id="../escape", model="model")


def test_v2_profile_loads_reasoning_disabled_and_writes_v3(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {
                    "anthropic": {
                        "protocol": "anthropic-messages",
                        "defaultModel": "claude-test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = ProviderProfileStore(path)

    loaded = store.load()["anthropic"]
    store.write((loaded,))

    assert loaded.reasoning == ReasoningConfig(enabled=False)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3


def test_profile_round_trips_model_limits_and_absolute_compact_threshold(
    tmp_path: Path,
) -> None:
    store = ProviderProfileStore(tmp_path / "providers.json")
    profile = ProviderProfile(
        id="openai",
        protocol=ProviderProtocol.OPENAI_RESPONSES,
        model="custom",
        limits=ModelLimits(100_000, 80_000, 20_000),
        compact=CompactConfig(60_000),
    )

    store.write((profile,))

    assert store.load()["openai"] == profile


def test_profile_rejects_compact_threshold_above_known_input_limit() -> None:
    with pytest.raises(ProviderProfileError, match="exceeds"):
        ProviderProfile(
            id="openai",
            model="custom",
            limits=ModelLimits(max_input_tokens=10_000),
            compact=CompactConfig(10_001),
        )
