"""把 MCP OAuth 注册信息和令牌保存在独立的用户私有文件中。"""

from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class McpOAuthTokenStore:
    """每个端点使用独立文件，避免不同连接相互覆盖凭据。"""

    def __init__(self, root: Path, server_name: str, url: str) -> None:
        digest = sha256(f"{server_name}\0{url}".encode()).hexdigest()
        self.path = root / f"{digest}.json"

    async def get_tokens(self) -> OAuthToken | None:
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value is not None else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._read().get("clientInfo")
        return (
            OAuthClientInformationFull.model_validate(value)
            if value is not None
            else None
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write("clientInfo", client_info.model_dump(mode="json"))

    def delete(self) -> bool:
        if self.path.parent.is_symlink():
            raise ValueError("MCP OAuth credential directory must not be a symlink")
        if self.path.is_symlink():
            raise ValueError("MCP OAuth credential path must not be a symlink")
        if not self.path.exists():
            return False
        self.path.unlink()
        return True

    def _read(self) -> dict[str, object]:
        if self.path.parent.is_symlink():
            raise ValueError("MCP OAuth credential directory must not be a symlink")
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ValueError("MCP OAuth credential path must not be a symlink")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("MCP OAuth credential file is invalid")
        return raw

    def _write(self, key: str, value: object) -> None:
        root = self.path.parent
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("MCP OAuth credential directory must not be a symlink")
        os.chmod(root, 0o700)
        data = self._read()
        data.update(version=1, **{key: value})
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=root, delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


__all__ = ["McpOAuthTokenStore"]
