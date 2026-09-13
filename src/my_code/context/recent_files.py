"""Full compact 后近期文件恢复的 provider-neutral 端口。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecentFileRecoveryReceipt:
    """一次性确认句柄；具体文件版本只由 runtime 实现保存。"""

    token: str
    session_key: str
    summary_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedRecentFile:
    """已按当前模型预算准备、可直接转为 durable attachment 的文本。"""

    path: str
    text: str
    sha256: str
    total_lines: int
    start_line: int
    end_line: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class PreparedRecentFileRecovery:
    files: tuple[PreparedRecentFile, ...]
    receipt: RecentFileRecoveryReceipt


class PostCompactFileRecovery(Protocol):
    """摘要后准备当前文件快照，并在 Session 提交后确认授权。"""

    async def prepare(
        self, session_key: str, summary_sha256: str
    ) -> PreparedRecentFileRecovery: ...

    async def acknowledge(self, receipt: RecentFileRecoveryReceipt) -> None: ...

    def discard(self, receipt: RecentFileRecoveryReceipt) -> None: ...


__all__ = [
    "PostCompactFileRecovery",
    "PreparedRecentFile",
    "PreparedRecentFileRecovery",
    "RecentFileRecoveryReceipt",
]
