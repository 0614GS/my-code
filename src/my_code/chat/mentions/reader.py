"""Bounded workspace reads used only by explicit Chat file mentions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from my_code.permissions.path_rules import read_denied
from my_code.permissions.policy import PermissionPolicy
from my_code.workspace.local import Workspace, WorkspaceBoundaryError

_MAX_READ_BYTES = 8 * 1024 * 1024
_DEFAULT_LINE_LIMIT = 2000
_DIRECTORY_LIMIT = 500


class AttachmentReadError(ValueError):
    """A mention cannot safely be represented as text context."""


@dataclass(frozen=True, slots=True)
class WorkspaceAttachment:
    path: str
    is_directory: bool
    body: str


class WorkspaceAttachmentReader:
    """Read mentions without invoking model tools or their execution pipeline."""

    def __init__(self, root: Path, policy: PermissionPolicy) -> None:
        self.workspace = Workspace(root)
        # PermissionPolicy is mutable; retaining it makes settings updates visible
        # to the next mention without rebuilding this reader.
        self.policy = policy

    async def read(
        self,
        raw_path: str,
        *,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> WorkspaceAttachment:
        return self._read_sync(raw_path, line_start=line_start, line_end=line_end)

    def _read_sync(
        self,
        raw_path: str,
        *,
        line_start: int | None,
        line_end: int | None,
    ) -> WorkspaceAttachment:
        try:
            path = self.workspace.resolve(raw_path, must_exist=True)
        except (OSError, WorkspaceBoundaryError) as error:
            raise AttachmentReadError(str(error)) from error
        display = self.workspace.display(path)
        if path.is_dir():
            if line_start is not None or line_end is not None:
                raise AttachmentReadError("Directories do not support line ranges")
            if read_denied(self.policy.rules, "Glob", display or "."):
                raise AttachmentReadError(f"Reading {display} is denied by rule")
            return WorkspaceAttachment(display, True, self._list_directory(path))
        if not path.is_file():
            raise AttachmentReadError(f"Not a file: {display}")
        if read_denied(self.policy.rules, "Read", display or "."):
            raise AttachmentReadError(f"Reading {display} is denied by rule")
        return WorkspaceAttachment(
            display,
            False,
            self._read_file(path, line_start=line_start, line_end=line_end),
        )

    def _read_file(
        self,
        path: Path,
        *,
        line_start: int | None,
        line_end: int | None,
    ) -> str:
        if path.stat().st_size > _MAX_READ_BYTES:
            raise AttachmentReadError("File exceeds 8 MiB read limit")
        raw = self.workspace.read_bytes(path)
        if len(raw) > _MAX_READ_BYTES:
            raise AttachmentReadError("File exceeds 8 MiB read limit")
        if b"\x00" in raw:
            raise AttachmentReadError("Binary files are not supported")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise AttachmentReadError("File is not valid UTF-8 text") from error

        offset = line_start or 1
        limit = line_end - offset + 1 if line_end is not None else _DEFAULT_LINE_LIMIT
        if offset < 1 or limit < 1:
            raise AttachmentReadError("Invalid line range")
        selected = lines[offset - 1 : offset - 1 + limit]
        if not selected:
            content = "<no lines in requested range>"
        else:
            content = "\n".join(
                f"{number:>6}\t{line}"
                for number, line in enumerate(selected, start=offset)
            )
        if line_start is None and offset - 1 + len(selected) < len(lines):
            content += "\n<file content truncated at 2000 lines; use Read for more>"
        return content

    def _list_directory(self, path: Path) -> str:
        entries: list[str] = []
        for child in path.iterdir():
            try:
                resolved = self.workspace.resolve(str(child), must_exist=True)
            except (OSError, WorkspaceBoundaryError):
                continue
            display = self.workspace.display(resolved)
            entries.append(f"{display}/" if resolved.is_dir() else display)
        entries.sort()
        truncated = len(entries) > _DIRECTORY_LIMIT
        selected = entries[:_DIRECTORY_LIMIT]
        content = "\n".join(selected) if selected else "<empty directory>"
        if truncated:
            content += "\n<directory listing truncated at 500 entries>"
        return content


__all__ = [
    "AttachmentReadError",
    "WorkspaceAttachment",
    "WorkspaceAttachmentReader",
]
