"""Canonical workspace paths and concrete local I/O."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceBoundaryError(ValueError):
    """A requested path is missing or escapes the configured workspace."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Concrete local workspace with one canonical path boundary."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes the workspace: {raw_path}")
        if must_exist and not resolved.exists():
            raise WorkspaceBoundaryError(f"Path does not exist: {raw_path}")
        return resolved

    def display(self, path: Path) -> str:
        return self._checked(path).relative_to(self.root).as_posix()

    def read_bytes(self, path: Path) -> bytes:
        return self._checked(path).read_bytes()

    def read_text(self, path: Path) -> str:
        return self._checked(path).read_text(encoding="utf-8")

    def write_text(
        self, path: Path, content: str, *, create_parents: bool = False
    ) -> None:
        checked = self._checked(path)
        if create_parents:
            checked.parent.mkdir(parents=True, exist_ok=True)
            checked = self._checked(checked)
        checked.write_text(content, encoding="utf-8")

    def _checked(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes the workspace: {path}")
        return resolved


__all__ = [
    "Workspace",
    "WorkspaceBoundaryError",
]
