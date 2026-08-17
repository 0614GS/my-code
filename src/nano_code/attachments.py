"""TUI ``@path`` mentions, workspace suggestions, and live attachments."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from nano_code.messages import AttachmentToolExchange, ContextAttachment, JsonObject
from nano_code.permissions import PermissionBehavior, PermissionPolicy
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.base import ToolExecutionError, ToolInputError
from nano_code.tools.paths import relative_display_path, resolve_workspace_path

_READ_LIMIT = 2000
_DIRECTORY_LIMIT = 500
_INDEX_LIMIT = 10_000
_EXCLUDED_ROOTS = frozenset({".git", ".nano-code", ".venv"})


@dataclass(frozen=True, slots=True)
class FileMention:
    """One syntactically valid file mention in its original prompt."""

    path: str
    raw: str
    start: int
    end: int
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class PathSuggestion:
    path: str
    is_directory: bool

    @property
    def display(self) -> str:
        return f"{self.path}/" if self.is_directory else self.path


@dataclass(frozen=True, slots=True)
class LoadedAttachment:
    attachment: ContextAttachment
    path: str
    is_directory: bool

    @property
    def display(self) -> str:
        action = "Listed directory" if self.is_directory else "Read"
        return f"{action} {self.path}"


def parse_file_mentions(prompt: str) -> tuple[FileMention, ...]:
    """Parse valid mentions in first-seen order, ignoring email-like ``@`` text."""

    mentions: list[FileMention] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    index = 0
    while index < len(prompt):
        at = prompt.find("@", index)
        if at < 0:
            break
        if at > 0 and not (prompt[at - 1].isspace() or prompt[at - 1] in "([{,:"):
            index = at + 1
            continue
        cursor = at + 1
        if cursor >= len(prompt):
            break
        if prompt[cursor] == '"':
            close = prompt.find('"', cursor + 1)
            if close < 0:
                index = cursor + 1
                continue
            path = prompt[cursor + 1 : close]
            cursor = close + 1
        else:
            begin = cursor
            while cursor < len(prompt) and not prompt[cursor].isspace():
                cursor += 1
            path = prompt[begin:cursor]

        line_start: int | None = None
        line_end: int | None = None
        range_at = path.rfind("#L")
        if range_at >= 0:
            parsed = _parse_line_range(path[range_at + 2 :])
            if parsed is None:
                index = cursor
                continue
            path = path[:range_at]
            line_start, line_end = parsed
        elif cursor < len(prompt) and prompt.startswith("#L", cursor):
            range_end = cursor + 2
            while range_end < len(prompt) and (
                prompt[range_end].isdigit() or prompt[range_end] == "-"
            ):
                range_end += 1
            parsed = _parse_line_range(prompt[cursor + 2 : range_end])
            if parsed is None:
                index = range_end
                continue
            line_start, line_end = parsed
            cursor = range_end

        if not path or path.startswith("@"):
            index = cursor
            continue
        key = (path, line_start, line_end)
        if key not in seen:
            seen.add(key)
            mentions.append(
                FileMention(
                    path=path,
                    raw=prompt[at:cursor],
                    start=at,
                    end=cursor,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
        index = cursor
    return tuple(mentions)


def mention_at_cursor(value: str, cursor: int) -> tuple[int, int, str] | None:
    """Return the replaceable unquoted mention fragment under the cursor."""

    prefix = value[:cursor]
    at = prefix.rfind("@")
    if at < 0 or (at > 0 and not (value[at - 1].isspace() or value[at - 1] in "([{,:")):
        return None
    fragment = prefix[at + 1 :]
    if fragment.startswith('"'):
        fragment = fragment[1:]
        if '"' in fragment:
            return None
        close = value.find('"', cursor)
        end = close + 1 if close >= 0 else cursor
    elif any(character.isspace() for character in fragment):
        return None
    else:
        end = cursor
        while end < len(value) and not value[end].isspace():
            end += 1
    return at, end, fragment


def format_path_mention(path: str) -> str:
    if any(character.isspace() for character in path):
        return f'@"{path}"'
    return f"@{path}"


class AttachmentLoader:
    """Load explicit workspace mentions through current tools and permissions."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        context: ToolContext,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._context = context

    async def load(self, prompt: str) -> tuple[LoadedAttachment, ...]:
        loaded: list[LoadedAttachment] = []
        for mention in parse_file_mentions(prompt):
            item = await self._load_one(mention)
            if item is not None:
                loaded.append(item)
        return tuple(loaded)

    async def _load_one(self, mention: FileMention) -> LoadedAttachment | None:
        try:
            path = resolve_workspace_path(
                self._context.cwd, mention.path, must_exist=True
            )
            is_directory = path.is_dir()
            if is_directory and mention.line_start is not None:
                return None
            tool_name = "Glob" if is_directory else "Read"
            tool = self._registry.get(tool_name)
            if tool is None or (not is_directory and not path.is_file()):
                return None
            display_path = relative_display_path(self._context.cwd, path)
            tool_input: JsonObject
            if is_directory:
                tool_input = {
                    "pattern": "*",
                    "path": display_path or ".",
                    "limit": _DIRECTORY_LIMIT,
                }
            else:
                offset = mention.line_start or 1
                limit = (
                    mention.line_end - offset + 1
                    if mention.line_end is not None
                    else _READ_LIMIT
                )
                tool_input = {"path": display_path, "offset": offset, "limit": limit}
            tool.validate_input(tool_input)
            decision = await self._policy.decide_explicit_user_read(
                tool, tool_input, self._context
            )
            if decision.behavior is not PermissionBehavior.ALLOW:
                return None
            effective_input = decision.updated_input or tool_input
            output = await tool.execute(effective_input, self._context)
            result = tool.to_model_result(output)
            if output.metadata.get("truncated") is True and (
                is_directory or mention.line_start is None
            ):
                if is_directory:
                    result += "\n<directory listing truncated at 500 entries>"
                else:
                    result += "\n<file content truncated; use Read for more lines>"
            exchange = AttachmentToolExchange(tool_name, effective_input, result)
            return LoadedAttachment(
                ContextAttachment(
                    source=f"file-mention:{display_path}",
                    content=(exchange,),
                    retention="live_session",
                ),
                display_path,
                is_directory,
            )
        except (OSError, UnicodeError, ValueError, ToolInputError, ToolExecutionError):
            return None


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
                entries = [PathSuggestion(path, False) for path in paths]
                entries.extend(PathSuggestion(path, True) for path in directories)
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
                    PathSuggestion(base.relative_to(self._cwd).as_posix(), True)
                )
            for filename in sorted(files):
                path = (base / filename).relative_to(self._cwd).as_posix()
                entries.append(PathSuggestion(path, False))
                if len(entries) >= _INDEX_LIMIT:
                    directories.clear()
                    return tuple(entries)
            if len(entries) + len(directories) >= _INDEX_LIMIT:
                directories.clear()
                return tuple(entries)
        return tuple(entries)


def _parse_line_range(value: str) -> tuple[int, int] | None:
    parts = value.split("-", 1)
    if not parts[0].isdigit():
        return None
    start = int(parts[0])
    end = start
    if len(parts) == 2:
        if not parts[1].isdigit():
            return None
        end = int(parts[1])
    if start < 1 or end < start or end - start + 1 > 5000:
        return None
    return start, end


def _is_excluded(path: str) -> bool:
    return any(part in _EXCLUDED_ROOTS for part in Path(path).parts)


async def _stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()


__all__ = [
    "AttachmentLoader",
    "FileMention",
    "LoadedAttachment",
    "PathSuggestion",
    "WorkspacePathSuggester",
    "format_path_mention",
    "mention_at_cursor",
    "parse_file_mentions",
]
