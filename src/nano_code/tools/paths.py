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

    # resolve(strict=False) follows every existing symlink component while still
    # allowing a not-yet-created final path. Checking before resolution would let
    # an in-workspace symlink point writes outside the workspace.
    resolved = candidate.resolve(strict=False)

    # Path containment is a tool safety invariant and is enforced even when the
    # permission policy is in bypass mode.
    if not resolved.is_relative_to(root):
        raise ToolInputError(f"Path escapes the workspace: {raw_path}")
    if must_exist and not resolved.exists():
        raise ToolInputError(f"Path does not exist: {raw_path}")

    if writable:
        # Agent state, VCS metadata, and the upstream snapshot are never writable
        # tool targets; permission approval cannot override this boundary.
        relative = resolved.relative_to(root)
        if relative.parts and relative.parts[0] in _PROTECTED_WRITE_ROOTS:
            raise ToolInputError(f"Path is protected from agent writes: {raw_path}")
    return resolved


def relative_display_path(cwd: Path, path: Path) -> str:
    """Return a stable workspace-relative display path."""

    return path.resolve().relative_to(cwd.resolve()).as_posix()
