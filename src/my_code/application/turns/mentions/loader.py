"""Load submitted file mentions into live context attachments."""

from my_code.application.contracts.inputs import FileMention, LoadedAttachment
from my_code.application.turns.mentions.parser import parse_file_mentions
from my_code.application.turns.mentions.reader import WorkspaceAttachmentReader
from my_code.conversation.attachments import FileMentionAttachment


class AttachmentLoader:
    """Load explicit mentions through the dedicated workspace reader."""

    def __init__(self, reader: WorkspaceAttachmentReader) -> None:
        self._reader = reader

    async def load(self, prompt: str) -> tuple[LoadedAttachment, ...]:
        loaded: list[LoadedAttachment] = []
        for mention in parse_file_mentions(prompt):
            item = await self._load_one(mention)
            if item is not None:
                loaded.append(item)
        return tuple(loaded)

    async def _load_one(self, mention: FileMention) -> LoadedAttachment | None:
        try:
            loaded = await self._reader.read(
                mention.path,
                line_start=mention.line_start,
                line_end=mention.line_end,
            )
            return LoadedAttachment(
                FileMentionAttachment(
                    path=loaded.path,
                    body=loaded.body,
                    is_directory=loaded.is_directory,
                ),
                loaded.path,
                loaded.is_directory,
            )
        except Exception:
            # One unavailable mention must not suppress later explicit mentions.
            # asyncio.CancelledError derives from BaseException and still propagates.
            return None


__all__ = [
    "AttachmentLoader",
]
