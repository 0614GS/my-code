"""SKILL-01: deterministic layers, diagnostics, and safe lazy loading."""

from pathlib import Path

import pytest

from my_code.skills.catalog import SkillCatalog
from my_code.skills.discovery import SkillSearchRoot, discover_skills
from my_code.skills.models import (
    SkillDiagnosticCode,
    SkillLoadError,
    SkillSourceId,
    SkillSourceKind,
)


def _source(priority: int, kind: SkillSourceKind) -> SkillSourceId:
    return SkillSourceId(priority, kind, kind.value)


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "metadata description",
    body: str = "PRIVATE INSTRUCTION BODY",
    allowed_tools: str | None = None,
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    fields = ["---"]
    if name is not None:
        fields.append(f"name: {name}")
    fields.append(f"description: {description}")
    if allowed_tools is not None:
        fields.append(f"allowed-tools: {allowed_tools}")
    fields.extend(("---", body))
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(fields) + "\n", encoding="utf-8")
    return path


def _catalog(*roots: SkillSearchRoot) -> SkillCatalog:
    catalog = SkillCatalog()
    catalog.commit(catalog.stage_scans(discover_skills(root) for root in roots))
    return catalog


def test_project_overrides_user_and_builtin_without_loading_body(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    _write_skill(builtin, "shared", description="builtin", body="BUILTIN SECRET")
    _write_skill(user, "shared", description="user", body="USER SECRET")
    _write_skill(project, "shared", description="project", body="PROJECT SECRET")
    catalog = _catalog(
        SkillSearchRoot(_source(100, SkillSourceKind.BUILTIN), builtin),
        SkillSearchRoot(_source(200, SkillSourceKind.USER), user),
        SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), project),
    )

    snapshot = catalog.snapshot()

    assert snapshot.get("shared") is not None
    assert snapshot.get("shared").description == "project"  # type: ignore[union-attr]
    assert snapshot.get("shared").inline_body is None  # type: ignore[union-attr]
    assert "PROJECT SECRET" not in repr(snapshot)
    loaded = snapshot.load("shared")
    assert loaded.instructions == "PROJECT SECRET"
    assert loaded.source.kind is SkillSourceKind.PROJECT


def test_same_layer_conflict_is_diagnostic_and_lower_layer_remains_visible(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user"
    project = tmp_path / "project"
    _write_skill(user, "fallback", name="duplicate", description="user")
    _write_skill(project, "first", name="duplicate", description="first")
    _write_skill(project, "second", name="duplicate", description="second")
    catalog = _catalog(
        SkillSearchRoot(_source(200, SkillSourceKind.USER), user),
        SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), project),
    )

    snapshot = catalog.snapshot()

    assert snapshot.get("duplicate").description == "user"  # type: ignore[union-attr]
    conflicts = [
        item
        for item in snapshot.diagnostics
        if item.code is SkillDiagnosticCode.SAME_LAYER_CONFLICT
    ]
    assert len(conflicts) == 2
    assert [item.locator for item in conflicts] == sorted(
        item.locator for item in conflicts
    )


def test_invalid_frontmatter_and_missing_body_do_not_hide_valid_skill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "valid")
    invalid = _write_skill(root, "invalid")
    invalid.write_text(
        "---\ndescription: bad\nexecute: ./payload.sh\n---\nbody\n",
        encoding="utf-8",
    )
    _write_skill(root, "empty", body="   ")

    scan = discover_skills(SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), root))
    catalog = SkillCatalog()
    catalog.commit(catalog.stage_scans((scan,)))

    assert [entry.name for entry in catalog.snapshot().entries] == ["valid"]
    assert {item.code for item in scan.diagnostics} == {
        SkillDiagnosticCode.INVALID_FRONTMATTER,
        SkillDiagnosticCode.MISSING_BODY,
    }


def test_symlinked_directory_and_skill_file_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    _write_skill(outside, "external")
    root.mkdir()
    (root / "escaped-directory").symlink_to(
        outside / "external", target_is_directory=True
    )
    linked_file = root / "linked-file"
    linked_file.mkdir()
    (linked_file / "SKILL.md").symlink_to(outside / "external" / "SKILL.md")
    _write_skill(root, "valid")

    scan = discover_skills(SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), root))

    assert [entry.name for entry in scan.entries] == ["valid"]
    assert [item.code for item in scan.diagnostics] == [
        SkillDiagnosticCode.SYMLINK,
        SkillDiagnosticCode.SYMLINK,
    ]


def test_indexed_file_must_be_reloaded_after_change(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    path = _write_skill(root, "mutable", body="old body")
    catalog = _catalog(SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), root))
    path.write_text(
        "---\ndescription: metadata description\n---\nnew body with size change\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillLoadError, match="reload"):
        catalog.snapshot().load("mutable")


def test_discovery_and_load_never_execute_neighboring_code(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "data-only")
    skill_dir = root / "data-only"
    marker = tmp_path / "executed"
    (skill_dir / "payload.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    (skill_dir / "payload.sh").write_text(
        f"touch {marker}\n",
        encoding="utf-8",
    )
    catalog = _catalog(SkillSearchRoot(_source(300, SkillSourceKind.PROJECT), root))

    catalog.snapshot().load("data-only")

    assert not marker.exists()
