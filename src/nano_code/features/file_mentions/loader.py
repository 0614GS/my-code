"""Load submitted file mentions into live context attachments."""

from nano_code.context.attachments.models import (
    AttachmentToolExchange,
    ContextAttachment,
)
from nano_code.conversation import JsonObject, ToolCall
from nano_code.conversation.primitives import new_id
from nano_code.features.file_mentions.models import FileMention, LoadedAttachment
from nano_code.features.file_mentions.parser import parse_file_mentions
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.invocation import ToolInvocation
from nano_code.tools.paths import relative_display_path, resolve_workspace_path

_READ_LIMIT = 2000
_DIRECTORY_LIMIT = 500


class AttachmentLoader:
    """Load explicit workspace mentions through current tools and permissions."""

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor

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
                self._executor.context.cwd, mention.path, must_exist=True
            )
            is_directory = path.is_dir()
            if is_directory and mention.line_start is not None:
                return None
            tool_name = "Glob" if is_directory else "Read"
            tool = self._executor.registry.get(tool_name)
            if tool is None or (not is_directory and not path.is_file()):
                return None
            display_path = relative_display_path(self._executor.context.cwd, path)
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
            outcome = await self._executor.execute(
                ToolCall(new_id(), tool_name, tool_input),
                invocation=ToolInvocation.explicit_file_mention(),
            )
            if outcome.result.is_error:
                return None
            effective_input = outcome.approved_input or tool_input
            result = outcome.result.content
            metadata = outcome.metadata or {}
            if metadata.get("truncated") is True and (
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
        except (OSError, UnicodeError, ValueError):
            return None
