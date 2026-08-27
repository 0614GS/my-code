"""Provider-neutral tool search mode shared by configuration and execution."""

from enum import StrEnum


class ToolSearchMode(StrEnum):
    DISPATCHER = "dispatcher"
    NATIVE = "native"


__all__ = ["ToolSearchMode"]
