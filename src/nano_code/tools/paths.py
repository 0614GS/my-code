"""文件系统工具共用的工作区路径解析。"""

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
    """解析路径，并拒绝通过遍历或符号链接逃逸 ``cwd``。"""

    root = cwd.resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    # resolve(strict=False) 会跟随所有已存在的符号链接组成部分，同时允许最终路径
    # 尚未创建。若在解析前检查，工作区内符号链接可能将写入指向工作区外。
    resolved = candidate.resolve(strict=False)

    # 路径包含关系是工具安全不变量，即使权限策略处于 bypass 模式也必须执行。
    if not resolved.is_relative_to(root):
        raise ToolInputError(f"Path escapes the workspace: {raw_path}")
    if must_exist and not resolved.exists():
        raise ToolInputError(f"Path does not exist: {raw_path}")

    if writable:
        # 智能体状态、VCS 元数据和上游源码快照永远不能作为可写工具目标；
        # 权限批准无法覆盖这条边界。
        relative = resolved.relative_to(root)
        if relative.parts and relative.parts[0] in _PROTECTED_WRITE_ROOTS:
            raise ToolInputError(f"Path is protected from agent writes: {raw_path}")
    return resolved


def relative_display_path(cwd: Path, path: Path) -> str:
    """返回稳定的工作区相对展示路径。"""

    return path.resolve().relative_to(cwd.resolve()).as_posix()
