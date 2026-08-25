"""JSON value types and strict normalization helpers."""

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]


def to_json_value(value: object) -> JsonValue:
    """Copy an object into the supported recursive JSON value representation."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = to_json_value(item)
        return result
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def to_json_object(value: object) -> JsonObject:
    """Copy a value into a JSON object, rejecting non-object roots."""

    converted = to_json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("Expected a JSON object")
    return converted


__all__ = ["JsonObject", "JsonValue", "to_json_object", "to_json_value"]
