"""Primitive values carried by conversation and tool content."""

from datetime import UTC, datetime
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid4())


__all__ = ["new_id", "utc_now"]
