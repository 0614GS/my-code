"""Application-owned Skill catalog, publication, and one-shot activation state."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from my_code.agent.capabilities import StepCapabilityContribution
from my_code.model.request import PromptStability, ResolvedPromptSection
from my_code.skills.catalog import (
    SkillCatalog,
    SkillCatalogSnapshot,
    SkillCatalogUpdate,
)
from my_code.skills.discovery import SkillSearchRoot, discover_skills
from my_code.skills.models import SkillDefinition, SkillDiagnostic, SkillSourceId
from my_code.skills.tool import SkillTool
from my_code.tools.catalog import ToolCatalog, ToolSourceId

_TOOL_SOURCE = ToolSourceId("feature", "skills")


@dataclass(frozen=True, slots=True)
class SkillActivation:
    id: str
    definition: SkillDefinition


class SkillRuntime:
    """Owns mutable Skill state without moving execution outside ToolExecutor."""

    def __init__(
        self,
        *,
        enabled: bool,
        roots: tuple[SkillSearchRoot, ...],
        tool_catalog: ToolCatalog,
        catalog: SkillCatalog | None = None,
    ) -> None:
        self.enabled = enabled
        self.roots = roots
        self.tool_catalog = tool_catalog
        self.catalog = catalog or SkillCatalog()
        self._pending: dict[str, dict[str, SkillActivation]] = {}
        self._started = False
        self._closed = False
        self._published = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return self.catalog.snapshot().diagnostics

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Skill runtime is closed")
        if self._started:
            return
        if self.enabled:
            self.reload()
        self._started = True

    def reload(self) -> SkillCatalogSnapshot:
        """Atomically publish a complete filesystem discovery result."""

        if self._closed:
            raise RuntimeError("Skill runtime is closed")
        if not self.enabled:
            return self.catalog.snapshot()
        update = self.catalog.stage_scans(discover_skills(root) for root in self.roots)
        return self._publish(update)

    def replace_source(
        self,
        source: SkillSourceId,
        definitions: tuple[SkillDefinition, ...],
    ) -> SkillCatalogSnapshot:
        """Normalize an MCP/other data source into the same catalog model."""

        if self._closed:
            raise RuntimeError("Skill runtime is closed")
        if not self.enabled:
            return self.catalog.snapshot()
        return self._publish(self.catalog.stage_definitions(source, definitions))

    def activate(
        self,
        run_id: str,
        snapshot: SkillCatalogSnapshot,
        name: str,
    ) -> SkillDefinition:
        if self._closed:
            raise RuntimeError("Skill runtime is closed")
        active = self._pending.setdefault(run_id, {})
        previous = active.get(name)
        if previous is not None:
            return previous.definition
        definition = snapshot.load(name)
        active[name] = SkillActivation(str(uuid4()), definition)
        return definition

    def capture(self, run_id: str) -> StepCapabilityContribution:
        activations = tuple(self._pending.get(run_id, {}).values())
        if not activations:
            return StepCapabilityContribution()
        restrictions = [
            frozenset(activation.definition.allowed_tools)
            for activation in activations
            if activation.definition.allowed_tools is not None
        ]
        allowlist = (
            restrictions[0].intersection(*restrictions[1:]) if restrictions else None
        )
        return StepCapabilityContribution(
            prompt_sections=tuple(
                ResolvedPromptSection(
                    key=f"skill:{activation.id}",
                    content=_render_activation(activation.definition),
                    stability=PromptStability.REQUEST,
                )
                for activation in activations
            ),
            tool_allowlist=allowlist,
            activation_ids=tuple(activation.id for activation in activations),
        )

    def acknowledge(self, run_id: str, activation_ids: tuple[str, ...]) -> None:
        active = self._pending.get(run_id)
        if active is None:
            return
        acknowledged = set(activation_ids)
        for name, activation in tuple(active.items()):
            if activation.id in acknowledged:
                del active[name]
        if not active:
            self._pending.pop(run_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        if self._published:
            self.tool_catalog.unregister_source(_TOOL_SOURCE)
            self._published = False

    def _publish(self, update: SkillCatalogUpdate) -> SkillCatalogSnapshot:
        if not update.changed and self._published:
            return self.catalog.snapshot()
        if update.snapshot.entries:
            self.tool_catalog.replace_source(
                _TOOL_SOURCE,
                (SkillTool(update.snapshot, self),),
            )
            self._published = True
        elif self._published:
            self.tool_catalog.unregister_source(_TOOL_SOURCE)
            self._published = False
        return self.catalog.commit(update)


def _render_activation(definition: SkillDefinition) -> str:
    compatibility = (
        f"\nCompatibility: {definition.compatibility}"
        if definition.compatibility is not None
        else ""
    )
    return (
        f"## Activated Skill: {definition.name}\n"
        f"Source: {definition.source}\n"
        f"Locator: {definition.locator}{compatibility}\n\n"
        f"{definition.instructions}"
    )


__all__ = [
    "SkillActivation",
    "SkillRuntime",
]
