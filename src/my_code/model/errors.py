"""Errors exposed by the provider-neutral model boundary."""


class ModelContextOverflow(RuntimeError):
    """The selected model rejected a request exceeding its context window."""


class ModelProtocolError(RuntimeError):
    """A provider response violated the normalized model protocol."""


class ModelStreamInterrupted(RuntimeError):
    """可安全重放的模型传输在完整终态到达前中断。"""

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


__all__ = [
    "ModelContextOverflow",
    "ModelProtocolError",
    "ModelStreamInterrupted",
]
