"""Private, credential-free cache for discovered provider model metadata."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from my_code.config.providers import atomic_private_json_write
from my_code.model.capabilities import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelLimits,
)


@dataclass(frozen=True, slots=True)
class CachedModelCatalog:
    fetched_at: str
    models: tuple[ModelDescriptor, ...]


class ModelCatalogCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def binding_key(profile_id: str, protocol: str, base_url: str | None) -> str:
        endpoint = (base_url or "<sdk-default>").rstrip("/").lower()
        return f"{profile_id}|{protocol}|{endpoint}"

    def load(self, binding_key: str) -> CachedModelCatalog | None:
        if not self.path.exists():
            return None
        try:
            root = json.loads(self.path.read_text(encoding="utf-8"))
            entry = root["bindings"].get(binding_key)
            if not isinstance(entry, dict):
                return None
            fetched_at = entry["fetchedAt"]
            values = entry["models"]
            if not isinstance(fetched_at, str) or not isinstance(values, list):
                return None
            models = tuple(_model_from_json(item) for item in values)
            return CachedModelCatalog(fetched_at, models)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

    def save(self, binding_key: str, models: tuple[ModelDescriptor, ...]) -> str:
        root: dict[str, object] = {"version": 1, "bindings": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == 1:
                    root = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        bindings = root.get("bindings")
        if not isinstance(bindings, dict):
            bindings = {}
            root["bindings"] = bindings
        fetched_at = datetime.now(UTC).isoformat()
        bindings[binding_key] = {
            "fetchedAt": fetched_at,
            "models": [_model_json(model) for model in models],
        }
        atomic_private_json_write(self.path, root)
        return fetched_at


def _model_json(model: ModelDescriptor) -> dict[str, object]:
    return {
        "id": model.id,
        "displayName": model.display_name,
        "limits": {
            "contextWindowTokens": model.limits.context_window_tokens,
            "maxInputTokens": model.limits.max_input_tokens,
            "maxOutputTokens": model.limits.max_output_tokens,
        },
        "capabilities": {
            "thinking": model.capabilities.thinking,
            "effort": model.capabilities.effort,
            "contextManagement": model.capabilities.context_management,
        },
    }


def _model_from_json(value: object) -> ModelDescriptor:
    if not isinstance(value, dict):
        raise ValueError("cached model must be an object")
    limits = value.get("limits")
    capabilities = value.get("capabilities")
    if not isinstance(limits, dict) or not isinstance(capabilities, dict):
        raise ValueError("cached model metadata is incomplete")
    model_id = value.get("id")
    display_name = value.get("displayName")
    if not isinstance(model_id, str) or not isinstance(display_name, str):
        raise ValueError("cached model identity is invalid")
    return ModelDescriptor(
        model_id,
        display_name,
        ModelLimits(
            _optional_int(limits.get("contextWindowTokens")),
            _optional_int(limits.get("maxInputTokens")),
            _optional_int(limits.get("maxOutputTokens")),
        ),
        ModelCapabilities(
            _optional_bool(capabilities.get("thinking")),
            _optional_bool(capabilities.get("effort")),
            _optional_bool(capabilities.get("contextManagement")),
        ),
        CapabilitySource.CACHE,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("cached model limit is invalid")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("cached model capability is invalid")


__all__ = [
    "CachedModelCatalog",
    "ModelCatalogCache",
]
