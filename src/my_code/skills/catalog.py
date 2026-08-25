"""Versioned Skill catalog with deterministic conflict resolution."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from my_code.skills.discovery import SkillScan
from my_code.skills.frontmatter import SkillDocumentError, split_document
from my_code.skills.models import (
    SkillDefinition,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillFingerprint,
    SkillIndexEntry,
    SkillLoadError,
    SkillSourceId,
)

_MAX_SKILL_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    version: int
    entries: tuple[SkillIndexEntry, ...]
    diagnostics: tuple[SkillDiagnostic, ...]
    _by_name: Mapping[str, SkillIndexEntry]

    @classmethod
    def build(
        cls,
        version: int,
        source_entries: Mapping[SkillSourceId, tuple[SkillIndexEntry, ...]],
        source_diagnostics: Mapping[SkillSourceId, tuple[SkillDiagnostic, ...]],
    ) -> SkillCatalogSnapshot:
        candidates = tuple(
            entry
            for source in sorted(source_entries)
            for entry in source_entries[source]
        )
        diagnostics = [
            diagnostic
            for source in sorted(source_diagnostics)
            for diagnostic in source_diagnostics[source]
        ]
        by_layer_name: dict[tuple[int, str], list[SkillIndexEntry]] = {}
        for entry in candidates:
            by_layer_name.setdefault((entry.source.priority, entry.name), []).append(
                entry
            )

        eligible: list[SkillIndexEntry] = []
        for (_, name), same_layer in sorted(by_layer_name.items()):
            if len(same_layer) == 1:
                eligible.extend(same_layer)
                continue
            locators = ", ".join(sorted(entry.locator for entry in same_layer))
            for entry in same_layer:
                diagnostics.append(
                    SkillDiagnostic(
                        SkillDiagnosticCode.SAME_LAYER_CONFLICT,
                        entry.source,
                        entry.locator,
                        f"Skill {name!r} has same-layer definitions: {locators}",
                    )
                )

        winners: dict[str, SkillIndexEntry] = {}
        for entry in sorted(
            eligible,
            key=lambda item: (-item.source.priority, item.name, item.locator),
        ):
            winners.setdefault(entry.name, entry)
        ordered = tuple(winners[name] for name in sorted(winners))
        ordered_diagnostics = tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    -item.source.priority,
                    item.source.kind.value,
                    item.source.name,
                    item.locator,
                    item.code.value,
                ),
            )
        )
        return cls(
            version,
            ordered,
            ordered_diagnostics,
            MappingProxyType(winners),
        )

    def get(self, name: str) -> SkillIndexEntry | None:
        return self._by_name.get(name)

    def load(self, name: str) -> SkillDefinition:
        entry = self.get(name)
        if entry is None:
            raise SkillLoadError(f"Unknown Skill: {name}")
        if entry.inline_body is not None:
            return SkillDefinition(
                entry.metadata,
                entry.inline_body,
                entry.source,
                entry.locator,
            )
        return _load_file_entry(entry)


@dataclass(frozen=True, slots=True)
class SkillCatalogUpdate:
    base_version: int
    snapshot: SkillCatalogSnapshot
    source_entries: Mapping[SkillSourceId, tuple[SkillIndexEntry, ...]]
    source_diagnostics: Mapping[SkillSourceId, tuple[SkillDiagnostic, ...]]
    changed: bool


class SkillCatalog:
    """Application-lifetime Skill index; mutations publish complete snapshots."""

    def __init__(self) -> None:
        self._version = 0
        self._source_entries: dict[SkillSourceId, tuple[SkillIndexEntry, ...]] = {}
        self._source_diagnostics: dict[SkillSourceId, tuple[SkillDiagnostic, ...]] = {}
        self._snapshot = SkillCatalogSnapshot.build(0, {}, {})

    @property
    def version(self) -> int:
        return self._version

    def snapshot(self) -> SkillCatalogSnapshot:
        return self._snapshot

    def stage_scans(self, scans: Iterable[SkillScan]) -> SkillCatalogUpdate:
        entries = dict(self._source_entries)
        diagnostics = dict(self._source_diagnostics)
        for scan in scans:
            if any(entry.source != scan.source for entry in scan.entries):
                raise ValueError("Skill scan entries must match their source")
            entries[scan.source] = scan.entries
            diagnostics[scan.source] = scan.diagnostics
        return self._stage(entries, diagnostics)

    def stage_definitions(
        self,
        source: SkillSourceId,
        definitions: Iterable[SkillDefinition],
    ) -> SkillCatalogUpdate:
        entries = dict(self._source_entries)
        diagnostics = dict(self._source_diagnostics)
        normalized: list[SkillIndexEntry] = []
        for definition in definitions:
            if definition.source != source:
                raise ValueError("Skill definition must match replacement source")
            normalized.append(
                SkillIndexEntry(
                    definition.metadata,
                    source,
                    definition.locator,
                    inline_body=definition.instructions,
                )
            )
        entries[source] = tuple(normalized)
        diagnostics[source] = ()
        return self._stage(entries, diagnostics)

    def commit(self, update: SkillCatalogUpdate) -> SkillCatalogSnapshot:
        if update.base_version != self._version:
            raise RuntimeError("Stale Skill catalog update")
        if not update.changed:
            return self._snapshot
        self._source_entries = dict(update.source_entries)
        self._source_diagnostics = dict(update.source_diagnostics)
        self._version = update.snapshot.version
        self._snapshot = update.snapshot
        return self._snapshot

    def _stage(
        self,
        entries: dict[SkillSourceId, tuple[SkillIndexEntry, ...]],
        diagnostics: dict[SkillSourceId, tuple[SkillDiagnostic, ...]],
    ) -> SkillCatalogUpdate:
        changed = (
            entries != self._source_entries or diagnostics != self._source_diagnostics
        )
        snapshot = (
            SkillCatalogSnapshot.build(self._version + 1, entries, diagnostics)
            if changed
            else self._snapshot
        )
        return SkillCatalogUpdate(
            self._version,
            snapshot,
            MappingProxyType(entries),
            MappingProxyType(diagnostics),
            changed,
        )


def _load_file_entry(entry: SkillIndexEntry) -> SkillDefinition:
    path = entry.path
    expected = entry.fingerprint
    if path is None or expected is None:
        raise SkillLoadError(f"Skill {entry.name!r} has no loadable content")
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise SkillLoadError("Indexed Skill path became a symbolic link")
        before = path.stat()
        if before.st_size > _MAX_SKILL_BYTES:
            raise SkillLoadError("Indexed Skill exceeds the 1 MiB size limit")
        if _fingerprint(before) != expected:
            raise SkillLoadError("Indexed Skill changed; reload before activation")
        content = path.read_text(encoding="utf-8")
        after = path.stat()
        if _fingerprint(after) != expected:
            raise SkillLoadError("Indexed Skill changed while it was loading")
        metadata, body = split_document(content, default_name=path.parent.name)
    except SkillLoadError:
        raise
    except (OSError, UnicodeError, SkillDocumentError) as error:
        raise SkillLoadError(
            f"Cannot load indexed Skill {entry.name!r}: {error}"
        ) from error
    if metadata != entry.metadata:
        raise SkillLoadError("Indexed Skill metadata changed; reload before activation")
    return SkillDefinition(metadata, body, entry.source, entry.locator)


def _fingerprint(stat: os.stat_result) -> SkillFingerprint:
    return SkillFingerprint(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


__all__ = [
    "SkillCatalog",
    "SkillCatalogSnapshot",
    "SkillCatalogUpdate",
]
