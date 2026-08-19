import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from my_code.model.capabilities import CapabilitySource, ModelDescriptor, ModelLimits
from my_code.providers.discovery import OpenAIModelCatalog
from my_code.providers.model_cache import ModelCatalogCache


class _Page:
    def __init__(self, ids: tuple[str, ...], next_page: "_Page | None" = None) -> None:
        self.data = tuple(SimpleNamespace(id=model_id) for model_id in ids)
        self._next_page = next_page

    def has_next_page(self) -> bool:
        return self._next_page is not None

    async def get_next_page(self) -> "_Page":
        assert self._next_page is not None
        return self._next_page


class _Models:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def list(self) -> _Page:
        return self.page


@pytest.mark.asyncio
async def test_openai_discovery_paginates_and_only_enriches_exact_official_ids() -> (
    None
):
    page = _Page(("gpt-4.1",), _Page(("vendor-gpt-4.1-custom",)))
    client = cast(Any, SimpleNamespace(models=_Models(page)))

    models = await OpenAIModelCatalog(client, official_endpoint=True).list_models()

    assert tuple(model.id for model in models) == (
        "gpt-4.1",
        "vendor-gpt-4.1-custom",
    )
    assert models[0].source is CapabilitySource.BUNDLED_CATALOG
    assert models[0].limits.context_window_tokens == 1_047_576
    assert models[1].source is CapabilitySource.PROVIDER_API
    assert not models[1].limits.known


def test_model_cache_is_private_and_isolates_normalized_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache" / "models.json"
    cache = ModelCatalogCache(path)
    model = ModelDescriptor("m", "Model", ModelLimits(max_input_tokens=123))
    key = cache.binding_key("work", "openai-responses", "HTTPS://EXAMPLE.COM/v1/")

    cache.save(key, (model,))

    assert (
        cache.load(
            cache.binding_key("work", "openai-responses", "https://example.com/v1")
        )
        is not None
    )
    assert (
        cache.load(
            cache.binding_key("other", "openai-responses", "https://example.com/v1")
        )
        is None
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert "apiKey" not in json.loads(path.read_text(encoding="utf-8"))
