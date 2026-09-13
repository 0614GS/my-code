"""近期工作区文件的 compact 后重读、预算与授权确认。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from my_code.context.attachments.projection import AttachmentProjector
from my_code.context.meter import ContextMeter
from my_code.context.recent_files import (
    PreparedRecentFile,
    PreparedRecentFileRecovery,
    RecentFileRecoveryReceipt,
)
from my_code.conversation.attachments import RecentFileSnapshotAttachment
from my_code.model.primitives import ContextFootprint, ProviderBinding
from my_code.tools.file_state import FileReadTracker, RecentFileRegistry
from my_code.workspace.local import (
    FileFingerprint,
    Workspace,
    WorkspaceBoundaryError,
    WorkspaceConflictError,
)

logger = logging.getLogger(__name__)

MAX_RECENT_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOKENS_PER_FILE = 5_000
MAX_TOTAL_TOKENS = 50_000
_MIN_TRUNCATED_BODY_TOKENS = 32


@dataclass(frozen=True, slots=True)
class _PendingFile:
    path: Path
    fingerprint: FileFingerprint
    total_lines: int
    complete: bool


@dataclass(frozen=True, slots=True)
class _PendingRecovery:
    receipt: RecentFileRecoveryReceipt
    files: tuple[_PendingFile, ...]


class RuntimeRecentFileRecovery:
    """组合运行期 registry、Workspace 与写入安全 tracker。"""

    def __init__(
        self,
        workspace: Workspace,
        recent_files: RecentFileRegistry,
        file_reads: FileReadTracker,
        meter: ContextMeter,
        binding: Callable[[], ProviderBinding],
    ) -> None:
        self._workspace = workspace
        self._recent_files = recent_files
        self._file_reads = file_reads
        self._meter = meter
        self._binding = binding
        self._projector = AttachmentProjector()
        self._pending: dict[str, _PendingRecovery] = {}

    async def prepare(
        self, session_key: str, summary_sha256: str
    ) -> PreparedRecentFileRecovery:
        """按近期顺序重读当前快照；单文件故障只跳过该候选。"""

        prepared: list[PreparedRecentFile] = []
        pending_files: list[_PendingFile] = []
        used_tokens = 0
        try:
            for candidate in self._recent_files.recent(session_key):
                if len(prepared) >= MAX_RECENT_FILES:
                    break
                loaded = await self._read_candidate(candidate.path)
                if loaded is None:
                    continue
                path, text, fingerprint = loaded
                remaining = MAX_TOTAL_TOKENS - used_tokens
                if remaining < 1:
                    break
                fitted = self._fit(
                    self._workspace.display(path),
                    text,
                    fingerprint.sha256,
                    min(MAX_TOKENS_PER_FILE, remaining),
                )
                if fitted is None:
                    break
                attachment, tokens = fitted
                prepared.append(
                    PreparedRecentFile(
                        attachment.path,
                        attachment.text,
                        attachment.sha256,
                        attachment.total_lines,
                        attachment.start_line,
                        attachment.end_line,
                        attachment.truncated,
                    )
                )
                pending_files.append(
                    _PendingFile(
                        path,
                        fingerprint,
                        attachment.total_lines,
                        not attachment.truncated,
                    )
                )
                used_tokens += tokens
                if attachment.truncated and remaining < MAX_TOKENS_PER_FILE:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected recent-file recovery preparation failure")

        receipt = RecentFileRecoveryReceipt(str(uuid4()), session_key, summary_sha256)
        self._pending[receipt.token] = _PendingRecovery(receipt, tuple(pending_files))
        return PreparedRecentFileRecovery(tuple(prepared), receipt)

    async def acknowledge(self, receipt: RecentFileRecoveryReceipt) -> None:
        """提交后清空旧授权，只恢复仍与准备版本一致的完整文件。"""

        pending = self._pending.pop(receipt.token, None)
        try:
            if pending is None or pending.receipt != receipt:
                self._file_reads.clear_session(receipt.session_key)
                logger.warning("Rejected unknown recent-file recovery receipt")
                return
            validated: list[_PendingFile] = []
            for item in pending.files:
                if not item.complete:
                    continue
                current = await self._current_fingerprint(item.path)
                if current != item.fingerprint:
                    logger.warning(
                        "Recent file changed before compact acknowledgement: %s",
                        item.path,
                    )
                    continue
                validated.append(item)
            self._file_reads.replace_session_complete(
                receipt.session_key,
                tuple(
                    (item.path, item.fingerprint, item.total_lines)
                    for item in validated
                ),
            )
        except asyncio.CancelledError:
            self._file_reads.clear_session(receipt.session_key)
            raise
        except Exception:
            self._file_reads.clear_session(receipt.session_key)
            logger.exception("Recent-file acknowledgement failed closed")

    def discard(self, receipt: RecentFileRecoveryReceipt) -> None:
        """未提交提案不改变原有读取授权。"""

        self._pending.pop(receipt.token, None)

    async def _read_candidate(
        self, raw_path: Path
    ) -> tuple[Path, str, FileFingerprint] | None:
        try:
            path = self._workspace.resolve(str(raw_path), must_exist=True)
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return None
            async with self._workspace.coordinator.path_lease(path, write=False):
                snapshot = self._workspace.read_snapshot(path)
            if len(snapshot.content) > MAX_FILE_BYTES or b"\x00" in snapshot.content:
                return None
            return path, snapshot.content.decode("utf-8"), snapshot.fingerprint
        except asyncio.CancelledError:
            raise
        except (
            FileNotFoundError,
            IsADirectoryError,
            OSError,
            UnicodeDecodeError,
            WorkspaceBoundaryError,
            WorkspaceConflictError,
        ) as error:
            logger.warning(
                "Skipping recent file %s during compact: %s", raw_path, error
            )
            return None

    async def _current_fingerprint(self, raw_path: Path) -> FileFingerprint | None:
        try:
            path = self._workspace.resolve(str(raw_path), must_exist=True)
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return None
            async with self._workspace.coordinator.path_lease(path, write=False):
                return self._workspace.read_snapshot(path).fingerprint
        except asyncio.CancelledError:
            raise
        except (
            FileNotFoundError,
            IsADirectoryError,
            OSError,
            WorkspaceBoundaryError,
            WorkspaceConflictError,
        ) as error:
            logger.warning(
                "Cannot validate recent file %s after compact: %s", raw_path, error
            )
            return None

    def _fit(
        self,
        path: str,
        text: str,
        sha256: str,
        token_limit: int,
    ) -> tuple[RecentFileSnapshotAttachment, int] | None:
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        complete = RecentFileSnapshotAttachment(
            path,
            text,
            sha256,
            total_lines,
            1,
            total_lines,
            False,
        )
        complete_tokens = self._tokens(complete)
        if complete_tokens <= token_limit:
            return complete, complete_tokens
        if total_lines < 2:
            return None

        low = 1
        high = total_lines - 1
        best: tuple[RecentFileSnapshotAttachment, int] | None = None
        while low <= high:
            count = (low + high) // 2
            candidate = RecentFileSnapshotAttachment(
                path,
                "".join(lines[:count]),
                sha256,
                total_lines,
                1,
                count,
                True,
            )
            tokens = self._tokens(candidate)
            if tokens <= token_limit:
                best = candidate, tokens
                low = count + 1
            else:
                high = count - 1
        if best is None:
            return None
        body_tokens = self._meter.estimate(
            self._binding(), ContextFootprint(best[0].text)
        ).tokens
        if body_tokens < _MIN_TRUNCATED_BODY_TOKENS:
            return None
        return best

    def _tokens(self, attachment: RecentFileSnapshotAttachment) -> int:
        projected = self._projector.project(attachment)
        text = "".join(getattr(block, "text", "") for block in projected.content)
        return self._meter.estimate(self._binding(), ContextFootprint(text)).tokens


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_RECENT_FILES",
    "MAX_TOKENS_PER_FILE",
    "MAX_TOTAL_TOKENS",
    "RuntimeRecentFileRecovery",
]
