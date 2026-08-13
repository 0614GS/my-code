"""Validation shared by provider profiles and resolved runtime settings."""

from urllib.parse import urlsplit


def validate_base_url(value: str) -> str:
    """Validate an Anthropic-compatible HTTP endpoint without contacting it."""

    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "base URL must be an http(s) URL without credentials, query, or fragment"
        )
    return normalized
