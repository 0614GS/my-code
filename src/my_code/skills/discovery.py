"""Deterministic, side-effect-free filesystem Skill discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from my_code.skills.frontmatter import SkillDocumentError, parse_frontmatter
from my_code.skills.models import (
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillFingerprint,
    SkillIndexEntry,
    SkillSourceId,
)

_MAX_SKILL_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SkillSearchRoot:
    source: SkillSourceId
    path: Path


@dataclass(frozen=True, slots=True)
class SkillScan:
    source: SkillSourceId
    entries: tuple[SkillIndexEntry, ...]
    diagnostics: tuple[SkillDiagnostic, ...]


def discover_skills(root: SkillSearchRoot) -> SkillScan:
    """Index one layer without retaining any instruction body."""

    if not root.path.exists():
        return SkillScan(root.source, (), ())
    try:
        canonical_root = root.path.resolve(strict=True)
        children = sorted(root.path.iterdir(), key=lambda item: item.name)
    except OSError as error:
        return SkillScan(
            root.source,
            (),
            (_diagnostic(root, root.path, SkillDiagnosticCode.IO_ERROR, error),),
        )

    entries: list[SkillIndexEntry] = []
    diagnostics: list[SkillDiagnostic] = []
    for skill_dir in children:
        if skill_dir.is_symlink():
            diagnostics.append(
                _diagnostic(
                    root,
                    skill_dir,
                    SkillDiagnosticCode.SYMLINK,
                    "Skill directories must not be symbolic links",
                )
            )
            continue
        if not skill_dir.is_dir():
            continue
        try:
            canonical_dir = skill_dir.resolve(strict=True)
        except OSError as error:
            diagnostics.append(
                _diagnostic(root, skill_dir, SkillDiagnosticCode.IO_ERROR, error)
            )
            continue
        if not canonical_dir.is_relative_to(canonical_root):
            diagnostics.append(
                _diagnostic(
                    root,
                    skill_dir,
                    SkillDiagnosticCode.PATH_ESCAPE,
                    "Skill directory resolves outside its search root",
                )
            )
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            diagnostics.append(
                _diagnostic(
                    root,
                    skill_file,
                    SkillDiagnosticCode.MISSING_FILE,
                    "Skill directory does not contain SKILL.md",
                )
            )
            continue
        if skill_file.is_symlink():
            diagnostics.append(
                _diagnostic(
                    root,
                    skill_file,
                    SkillDiagnosticCode.SYMLINK,
                    "SKILL.md must not be a symbolic link",
                )
            )
            continue
        try:
            canonical_file = skill_file.resolve(strict=True)
            if not canonical_file.is_relative_to(canonical_root):
                raise _PathEscape
            entry = _index_file(root.source, skill_file, skill_dir.name)
        except _PathEscape:
            diagnostics.append(
                _diagnostic(
                    root,
                    skill_file,
                    SkillDiagnosticCode.PATH_ESCAPE,
                    "SKILL.md resolves outside its search root",
                )
            )
        except SkillDocumentError as error:
            code = (
                SkillDiagnosticCode.MISSING_BODY
                if "body is missing" in str(error)
                else SkillDiagnosticCode.INVALID_FRONTMATTER
            )
            diagnostics.append(_diagnostic(root, skill_file, code, error))
        except (OSError, UnicodeError) as error:
            diagnostics.append(
                _diagnostic(root, skill_file, SkillDiagnosticCode.IO_ERROR, error)
            )
        else:
            entries.append(entry)
    return SkillScan(root.source, tuple(entries), tuple(diagnostics))


def _index_file(
    source: SkillSourceId,
    path: Path,
    default_name: str,
) -> SkillIndexEntry:
    before = path.stat()
    if before.st_size > _MAX_SKILL_BYTES:
        raise SkillDocumentError("SKILL.md exceeds the 1 MiB size limit")
    with path.open("r", encoding="utf-8") as handle:
        if handle.readline().strip() != "---":
            raise SkillDocumentError("SKILL.md must start with --- frontmatter")
        frontmatter: list[str] = []
        for line in handle:
            if line.strip() == "---":
                break
            frontmatter.append(line)
        else:
            raise SkillDocumentError("SKILL.md frontmatter is not terminated")
        metadata = parse_frontmatter(frontmatter, default_name=default_name)
        if not any(line.strip() for line in handle):
            raise SkillDocumentError("Skill instruction body is missing")
        opened = os.fstat(handle.fileno())
    after = path.stat()
    fingerprint = _fingerprint(after)
    if _fingerprint(before) != fingerprint or _fingerprint(opened) != fingerprint:
        raise OSError("SKILL.md changed while it was being indexed")
    return SkillIndexEntry(
        metadata=metadata,
        source=source,
        locator=str(path),
        path=path,
        fingerprint=fingerprint,
    )


def _fingerprint(stat: os.stat_result) -> SkillFingerprint:
    return SkillFingerprint(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


class _PathEscape(Exception):
    pass


def _diagnostic(
    root: SkillSearchRoot,
    path: Path,
    code: SkillDiagnosticCode,
    detail: object,
) -> SkillDiagnostic:
    return SkillDiagnostic(code, root.source, str(path), str(detail))


__all__ = [
    "SkillScan",
    "SkillSearchRoot",
    "discover_skills",
]
