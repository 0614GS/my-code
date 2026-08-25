"""Immutable capabilities captured for one Agent step."""

from dataclasses import dataclass, field
from typing import Protocol

from my_code.model.request import ResolvedPromptSection
from my_code.tools.catalog import ToolCatalogSnapshot


@dataclass(frozen=True, slots=True)
class StepCapabilityContribution:
    """One-shot additions supplied by an application-owned capability source."""

    prompt_sections: tuple[ResolvedPromptSection, ...] = ()
    tool_allowlist: frozenset[str] | None = None
    activation_ids: tuple[str, ...] = ()


class StepCapabilitySource(Protocol):
    """Application-owned source captured and acknowledged at step boundaries."""

    def capture(self, run_id: str) -> StepCapabilityContribution: ...

    def acknowledge(self, run_id: str, activation_ids: tuple[str, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class RunCapabilitySnapshot:
    """Capabilities shared by request planning and subsequent execution."""

    tools: ToolCatalogSnapshot
    prompt_sections: tuple[ResolvedPromptSection, ...] = ()
    activation_ids: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def capture(
        cls,
        tools: ToolCatalogSnapshot,
        contribution: StepCapabilityContribution | None = None,
    ) -> "RunCapabilitySnapshot":
        actual = contribution or StepCapabilityContribution()
        narrowed = (
            tools
            if actual.tool_allowlist is None
            else tools.select(actual.tool_allowlist)
        )
        return cls(narrowed, actual.prompt_sections, actual.activation_ids)


__all__ = [
    "RunCapabilitySnapshot",
    "StepCapabilityContribution",
    "StepCapabilitySource",
]
