"""Application-owned Skill catalog, publication, and one-shot activation state."""

from __future__ import annotations

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
        snapshot: SkillCatalogSnapshot,
        name: str,
    ) -> SkillDefinition:
        if self._closed:
            raise RuntimeError("Skill runtime is closed")
        return snapshot.load(name)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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


__all__ = [
    "SkillRuntime",
]
