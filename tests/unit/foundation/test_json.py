from pathlib import Path

import pytest

from my_code.foundation.json import to_json_object, to_json_value


def test_json_normalization_copies_nested_containers() -> None:
    nested = [1, {"enabled": True}]
    source = {"items": nested}

    normalized = to_json_object(source)

    assert normalized == source
    assert normalized is not source
    assert normalized["items"] is not nested


@pytest.mark.parametrize("value", [["not", "an", "object"], "text", None])
def test_json_object_rejects_non_object_roots(value: object) -> None:
    with pytest.raises(TypeError, match="Expected a JSON object"):
        to_json_object(value)


def test_json_normalization_rejects_non_string_object_keys() -> None:
    with pytest.raises(TypeError, match="JSON object keys must be strings"):
        to_json_value({1: "value"})


def test_json_normalization_rejects_unsupported_values() -> None:
    with pytest.raises(TypeError, match="Unsupported JSON value: PosixPath"):
        to_json_value(Path("README.md"))
