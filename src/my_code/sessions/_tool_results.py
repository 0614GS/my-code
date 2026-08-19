"""Private storage for oversized tool results owned by Session."""

import hashlib
import os
from pathlib import Path


class ToolResultStore:
    """Persist oversized output and return a bounded, stable transcript preview."""

    def __init__(self, root: Path, max_inline_chars: int = 20_000) -> None:
        self.root = root
        self.max_inline_chars = max_inline_chars

    def externalize(self, tool_use_id: str, content: str) -> str:
        if len(content) <= self.max_inline_chars:
            return content

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:20]
        path = self.root / f"{digest}.txt"
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)

        preview = content[: self.max_inline_chars]
        return (
            f"Output exceeded {self.max_inline_chars} characters and was saved to "
            f"{path}.\n\nPreview:\n{preview}"
        )


__all__: list[str] = []
