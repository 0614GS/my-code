"""Optional synchronous token-counting port."""

from typing import Protocol


class TokenCounter(Protocol):
    def count(self, model: str, text: str) -> int | None:
        """Return a token count, or ``None`` when the model is unsupported."""


class NullTokenCounter:
    def count(self, model: str, text: str) -> None:
        return None


__all__ = ["NullTokenCounter", "TokenCounter"]
