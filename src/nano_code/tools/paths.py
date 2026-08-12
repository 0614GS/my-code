"""Workspace path resolution shared by filesystem tools."""

from pathlib import Path

from nano_code.tools.base import ToolInputError

_PROTECTED_WRITE_ROOTS = frozenset({".git", ".nano-code", "claude-code"})


def resolve_workspace_path(
    cwd: Path,
    raw_path: str,
    *,
    must_exist: bool = False,
    writable: bool = False,
) -> Path:
    """Resolve a path and reject traversal or symlink escape from ``cwd``."""

    root = cwd.resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)

    if not resolved.is_relative_to(root):
        raise ToolInputError(f"Path escapes the workspace: {raw_path}")
    if must_exist and not resolved.exists():
        raise ToolInputError(f"Path does not exist: {raw_path}")

    if writable:
        relative = resolved.relative_to(root)
        if relative.parts and relative.parts[0] in _PROTECTED_WRITE_ROOTS:
            raise ToolInputError(f"Path is protected from agent writes: {raw_path}")
    return resolved


def relative_display_path(cwd: Path, path: Path) -> str:
    """Return a stable workspace-relative display path."""

    return path.resolve().relative_to(cwd.resolve()).as_posix()
