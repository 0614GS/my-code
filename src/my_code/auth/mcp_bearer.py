"""把 MCP Bearer 凭据按端点保存在用户私有目录。"""

from __future__ import annotations

import os
import tempfile
from hashlib import sha256
from pathlib import Path


class McpBearerTokenStore:
    def __init__(self, root: Path, server_name: str, url: str) -> None:
        digest = sha256(f"{server_name}\0{url}".encode()).hexdigest()
        self.path = root / f"{digest}.token"

    def load(self) -> str | None:
        if self.path.parent.is_symlink():
            raise ValueError("MCP Bearer credential directory must not be a symlink")
        if not self.path.exists() and not self.path.is_symlink():
            return None
        if self.path.is_symlink():
            raise ValueError("MCP Bearer credential path must not be a symlink")
        value = self.path.read_text(encoding="utf-8")
        if not value or any(character.isspace() for character in value):
            raise ValueError("MCP Bearer credential is invalid")
        return value

    def save(self, token: str) -> None:
        if not token or any(character.isspace() for character in token):
            raise ValueError("MCP Bearer token must be non-empty without whitespace")
        root = self.path.parent
        if root.is_symlink():
            raise ValueError("MCP Bearer credential directory must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        if self.path.is_symlink():
            raise ValueError("MCP Bearer credential path must not be a symlink")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=root, delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self) -> bool:
        if self.path.parent.is_symlink():
            raise ValueError("MCP Bearer credential directory must not be a symlink")
        if self.path.is_symlink():
            raise ValueError("MCP Bearer credential path must not be a symlink")
        if not self.path.exists():
            return False
        self.path.unlink()
        return True


__all__ = ["McpBearerTokenStore"]
