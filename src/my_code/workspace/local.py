"""Canonical workspace paths and concrete local I/O."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from my_code.workspace.coordination import WorkspaceCoordinator


class WorkspaceBoundaryError(ValueError):
    """A requested path is missing or escapes the configured workspace."""


class WorkspaceConflictError(RuntimeError):
    """文件自读取后发生变化，当前写入不得覆盖该版本。"""


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """标识一次稳定文件读取所得的具体版本。"""

    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """一次一致读取的内容与版本。"""

    content: bytes
    fingerprint: FileFingerprint


@dataclass(frozen=True, slots=True)
class Workspace:
    """Concrete local workspace with one canonical path boundary."""

    root: Path
    coordinator: WorkspaceCoordinator = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "coordinator", WorkspaceCoordinator(self.root))

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

    def read_snapshot(self, path: Path) -> FileSnapshot:
        """通过同一文件描述符读取内容，并拒绝读取期间发生的变化。"""

        checked = self._checked(path)
        with checked.open("rb") as stream:
            before = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        if _stat_identity(before) != _stat_identity(after):
            raise WorkspaceConflictError(f"File changed while reading: {path}")
        return FileSnapshot(content, _fingerprint(after, content))

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

    def atomic_write_text(
        self,
        path: Path,
        content: str,
        *,
        expected: FileFingerprint | None = None,
        must_not_exist: bool = False,
        create_parents: bool = False,
    ) -> FileSnapshot:
        """在同目录完整落盘后发布，并执行乐观版本校验。"""

        if expected is not None and must_not_exist:
            raise ValueError("expected and must_not_exist are mutually exclusive")
        checked = self._checked(path)
        if create_parents:
            checked.parent.mkdir(parents=True, exist_ok=True)
            checked = self._checked(checked)
        if not checked.parent.is_dir():
            raise FileNotFoundError(checked.parent)

        existing_mode: int | None = None
        if checked.exists():
            if not checked.is_file():
                raise IsADirectoryError(checked)
            existing_mode = stat.S_IMODE(checked.stat().st_mode)
        payload = content.encode("utf-8")
        temporary: Path | None = None
        try:
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{checked.name}.", suffix=".tmp", dir=checked.parent
            )
            temporary = Path(raw_temporary)
            if existing_mode is not None:
                os.fchmod(descriptor, existing_mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            if expected is not None:
                try:
                    current = self.read_snapshot(checked).fingerprint
                except FileNotFoundError as error:
                    raise WorkspaceConflictError(
                        f"File was removed since it was read: {self.display(checked)}"
                    ) from error
                if current != expected:
                    raise WorkspaceConflictError(
                        f"File changed since it was read: {self.display(checked)}"
                    )
                os.replace(temporary, checked)
                temporary = None
            elif must_not_exist:
                try:
                    os.link(temporary, checked)
                except FileExistsError as error:
                    raise WorkspaceConflictError(
                        f"File was created concurrently: {self.display(checked)}"
                    ) from error
            else:
                os.replace(temporary, checked)
                temporary = None

            _fsync_directory(checked.parent)
            return self.read_snapshot(checked)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _checked(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes the workspace: {path}")
        return resolved


__all__ = [
    "FileFingerprint",
    "FileSnapshot",
    "Workspace",
    "WorkspaceBoundaryError",
    "WorkspaceConflictError",
]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _fingerprint(value: os.stat_result, content: bytes) -> FileFingerprint:
    return FileFingerprint(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
