"""Errors exposed by the provider-neutral model boundary."""


class ModelContextOverflow(RuntimeError):
    """The selected model rejected a request exceeding its context window."""
