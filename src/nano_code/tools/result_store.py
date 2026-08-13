"""Externalize oversized tool text before it enters model context."""

import hashlib
import os
from pathlib import Path


class ToolResultStore:
    """Persist large outputs and return a bounded, stable preview."""

    def __init__(self, root: Path, max_inline_chars: int = 20_000) -> None:
        self.root = root
        self.max_inline_chars = max_inline_chars

    def externalize(self, tool_use_id: str, content: str) -> str:
        if len(content) <= self.max_inline_chars:
            return content

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        # Deriving the filename from protocol identity makes repeated projection
        # of the same result stable without exposing arbitrary model-generated IDs.
        digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:20]
        path = self.root / f"{digest}.txt"
        if not path.exists():
            # O_EXCL prevents an existing result from being silently overwritten;
            # restrictive modes keep potentially sensitive command output private.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)

        # The fixed prefix is stored in the transcript, so future requests replay
        # byte-stable context instead of re-deciding how much content to include.
        preview = content[: self.max_inline_chars]
        return (
            f"Output exceeded {self.max_inline_chars} characters and was saved to "
            f"{path}.\n\nPreview:\n{preview}"
        )
