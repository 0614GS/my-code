"""在超大工具文本进入模型上下文前将其外置。"""

import hashlib
import os
from pathlib import Path


class ToolResultStore:
    """持久化大型输出并返回有界、稳定的预览。"""

    def __init__(self, root: Path, max_inline_chars: int = 20_000) -> None:
        self.root = root
        self.max_inline_chars = max_inline_chars

    def externalize(self, tool_use_id: str, content: str) -> str:
        if len(content) <= self.max_inline_chars:
            return content

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

        # 从协议标识派生文件名，使同一结果的重复投影保持稳定，
        # 同时不暴露模型任意生成的 ID。
        digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:20]
        path = self.root / f"{digest}.txt"
        if not path.exists():
            # O_EXCL 防止已有结果被静默覆盖；严格权限模式保护可能敏感的命令输出。
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)

        # 固定前缀会写入会话记录，使未来请求重放字节级稳定的上下文，
        # 而不是重新决定包含多少内容。
        preview = content[: self.max_inline_chars]
        return (
            f"Output exceeded {self.max_inline_chars} characters and was saved to "
            f"{path}.\n\nPreview:\n{preview}"
        )
