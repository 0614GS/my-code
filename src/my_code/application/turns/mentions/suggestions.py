"""Git-aware workspace path suggestions for file mentions."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from pathlib import Path

from my_code.application.contracts.inputs import PathSuggestion

_INDEX_LIMIT = 10_000
_EXCLUDED_ROOTS = frozenset({".git", ".my-code", ".venv"})


class WorkspacePathSuggester:
    """Short-lived, bounded Git-aware workspace path index."""

    def __init__(self, cwd: Path, *, cache_seconds: float = 5.0) -> None:
        self._cwd = cwd
        self._cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._entries: tuple[PathSuggestion, ...] = ()
        self._lock = asyncio.Lock()

    async def suggest(self, query: str) -> tuple[PathSuggestion, ...]:
        entries = await self._index()
        normalized = query.removeprefix("./").casefold()

        def rank(entry: PathSuggestion) -> tuple[int, int, str]:
            value = entry.path.casefold()
            if value.startswith(normalized):
                match_rank = 0
            elif normalized in value:
                match_rank = 1
            else:
                match_rank = 2
            return match_rank, 0 if entry.is_directory else 1, value

        matches = (
            entry
            for entry in entries
            if not normalized or normalized in entry.path.casefold()
        )
        return tuple(sorted(matches, key=rank)[:20])

    async def _index(self) -> tuple[PathSuggestion, ...]:
        if time.monotonic() - self._cached_at < self._cache_seconds:
            return self._entries
        async with self._lock:
            if time.monotonic() - self._cached_at < self._cache_seconds:
                return self._entries
            paths = await self._git_paths()
            if paths is None:
                entries = list(await asyncio.to_thread(self._scan_entries))
            else:
                directories = {
                    parent.as_posix()
                    for value in paths
                    for parent in Path(value).parents
                    if parent.as_posix() != "."
                }
                entries = [PathSuggestion(path, False, path) for path in paths]
                entries.extend(
                    PathSuggestion(path, True, f"{path}/") for path in directories
                )
            self._entries = tuple(
                sorted(entries, key=lambda item: (item.path, item.is_directory))
            )
            self._cached_at = time.monotonic()
            return self._entries

    async def _git_paths(self) -> set[str] | None:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                cwd=self._cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2)
        except TimeoutError:
            await _stop_process(process)
            return None
        except OSError:
            return None
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        if process.returncode != 0:
            return None
        return {
            path
            for path in stdout.decode("utf-8", errors="replace").split("\0")
            if path and not _is_excluded(path)
        }

    def _scan_entries(self) -> tuple[PathSuggestion, ...]:
        entries: list[PathSuggestion] = []
        for root, directories, files in os.walk(self._cwd, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _EXCLUDED_ROOTS
            )
            base = Path(root)
            if base != self._cwd:
                entries.append(
                    PathSuggestion(
                        base.relative_to(self._cwd).as_posix(),
                        True,
                        f"{base.relative_to(self._cwd).as_posix()}/",
                    )
                )
            for filename in sorted(files):
                path = (base / filename).relative_to(self._cwd).as_posix()
                entries.append(PathSuggestion(path, False, path))
                if len(entries) >= _INDEX_LIMIT:
                    directories.clear()
                    return tuple(entries)
            if len(entries) + len(directories) >= _INDEX_LIMIT:
                directories.clear()
                return tuple(entries)
        return tuple(entries)


def _is_excluded(path: str) -> bool:
    return any(part in _EXCLUDED_ROOTS for part in Path(path).parts)


async def _stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()


__all__ = [
    "WorkspacePathSuggester",
]
