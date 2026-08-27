"""Private storage for oversized tool results owned by Session."""

import hashlib
import os
import stat
from pathlib import Path


class ToolResultStore:
    """Persist oversized output and return a bounded, stable transcript preview."""

    def __init__(self, root: Path, max_inline_chars: int = 20_000) -> None:
        self.root = root
        self.max_inline_chars = max_inline_chars

    def externalize(self, tool_use_id: str, content: str) -> str:
        if len(content) <= self.max_inline_chars:
            return content

        _ensure_private_directory(self.root)
        digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:20]
        path = self.root / f"{digest}.txt"
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        else:
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise FileExistsError(f"Unsafe tool-result path already exists: {path}")
            raise FileExistsError(f"Tool-result path already exists: {path}")

        preview = content[: self.max_inline_chars]
        return (
            f"Output exceeded {self.max_inline_chars} characters. The full output "
            f"was saved temporarily to {path}; it remains available only while "
            "that temporary file exists.\n\nPreview:\n"
            f"{preview}"
        )


def _ensure_private_directory(directory: Path) -> None:
    existing_parent = directory
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = directory
    while current != existing_parent:
        current.chmod(0o700)
        current = current.parent
    if existing_parent.name.startswith("my-code-"):
        existing_parent.chmod(0o700)


__all__: list[str] = []
