"""文件系统工具共用的工作区路径解析。"""

from pathlib import Path

from my_code.tools.base import ToolInputError
from my_code.workspace.local import Workspace, WorkspaceBoundaryError

_SENSITIVE_WRITE_ROOTS = frozenset({".git", ".my-code"})


def resolve_workspace_path(
    cwd: Path,
    raw_path: str,
    *,
    must_exist: bool = False,
    writable: bool = False,
) -> Path:
    """解析路径，并拒绝通过遍历或符号链接逃逸 ``cwd``。"""

    try:
        resolved = Workspace(cwd).resolve(raw_path, must_exist=must_exist)
    except WorkspaceBoundaryError as error:
        raise ToolInputError(str(error)) from error

    del writable  # 写入敏感路径由权限层强制询问；执行层只维持 cwd 边界。
    return resolved


def resolve_read_path(
    cwd: Path,
    raw_path: str,
    *,
    internal_root: Path | None = None,
    must_exist: bool = False,
) -> tuple[Path, bool]:
    """Resolve a workspace path or one path below the controlled temp root."""

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return resolve_workspace_path(cwd, raw_path, must_exist=must_exist), False
    resolved = candidate.resolve(strict=False)
    workspace_root = cwd.resolve(strict=False)
    if resolved.is_relative_to(workspace_root):
        if must_exist and not resolved.exists():
            raise ToolInputError(f"Path does not exist: {raw_path}")
        return resolved, False
    if internal_root is None:
        raise ToolInputError(f"Path escapes the workspace: {raw_path}")
    root = internal_root.resolve(strict=False)
    if ".." in candidate.parts or not resolved.is_relative_to(root):
        raise ToolInputError(
            f"Path is outside the controlled runtime temp root: {raw_path}"
        )
    if must_exist and not resolved.exists():
        raise ToolInputError(f"Path does not exist: {raw_path}")
    return resolved, True


def is_sensitive_write_path(cwd: Path, path: Path) -> bool:
    """返回路径是否属于需要当次人工确认的产品敏感命名空间。"""

    relative = path.resolve(strict=False).relative_to(cwd.resolve(strict=False))
    return bool(relative.parts and relative.parts[0] in _SENSITIVE_WRITE_ROOTS)


def relative_display_path(cwd: Path, path: Path) -> str:
    """返回稳定的工作区相对展示路径。"""

    return Workspace(cwd).display(path)
