"""Provider identifiers shared by settings, profiles, and credentials."""

import re

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_provider_id(value: str) -> str:
    """Validate and return an identifier safe for JSON object keys."""

    if _PROVIDER_ID.fullmatch(value) is None:
        raise ValueError("provider ID must match [a-z0-9][a-z0-9_-]{0,63}")
    return value
