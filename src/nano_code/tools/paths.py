"""文件系统工具共用的工作区路径解析。"""

from pathlib import Path

from nano_code.tools.base import ToolInputError

_SENSITIVE_WRITE_ROOTS = frozenset({".git", ".nano-code"})


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

    del writable  # 写入敏感路径由权限层强制询问；执行层只维持 cwd 边界。
    return resolved


def is_sensitive_write_path(cwd: Path, path: Path) -> bool:
    """返回路径是否属于需要当次人工确认的产品敏感命名空间。"""

    relative = path.resolve(strict=False).relative_to(cwd.resolve(strict=False))
    return bool(relative.parts and relative.parts[0] in _SENSITIVE_WRITE_ROOTS)


def relative_display_path(cwd: Path, path: Path) -> str:
    """返回稳定的工作区相对展示路径。"""

    return path.resolve().relative_to(cwd.resolve()).as_posix()
