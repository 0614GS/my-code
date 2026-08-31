"""Errors exposed by the provider-neutral model boundary."""


class ModelContextOverflow(RuntimeError):
    """The selected model rejected a request exceeding its context window."""


class ModelProtocolError(RuntimeError):
    """A provider response violated the normalized model protocol."""


__all__ = [
    "ModelContextOverflow",
    "ModelProtocolError",
]
