"""运行仓库完整的本地质量检查。"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("检查格式", "ruff", "format", "--check", "."),
    ("检查代码规范", "ruff", "check", "."),
    ("检查类型", "pyright"),
    ("检查模块依赖", "tach", "check"),
    ("运行测试", "pytest"),
)


def main() -> None:
    """按固定顺序运行检查，并在首个失败项停止。"""
    for label, *command in CHECKS:
        print(f"\n==> {label}", flush=True)
        subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


if __name__ == "__main__":
    main()
