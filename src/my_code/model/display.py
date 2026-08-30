"""Shared visibility tiers for provider-neutral presentation facts."""

from enum import IntEnum


class DisplayDensity(IntEnum):
    CONCISE = 1
    DETAILED = 2
    AUDIT = 3

    @classmethod
    def from_view_mode(cls, value: str) -> "DisplayDensity":
        try:
            return {"concise": cls.CONCISE, "detailed": cls.DETAILED}[value]
        except KeyError as error:
            raise ValueError(f"Unknown main view mode: {value}") from error

    @property
    def view_mode(self) -> str:
        if self is DisplayDensity.AUDIT:
            raise ValueError("Audit density is available only in the transcript")
        return "concise" if self is DisplayDensity.CONCISE else "detailed"

    def includes(self, minimum: "DisplayDensity") -> bool:
        return self >= minimum


__all__ = ["DisplayDensity"]
