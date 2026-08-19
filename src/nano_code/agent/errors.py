"""Agent and context planning errors."""


class ContextOverflow(RuntimeError):
    """上下文策略无法在当前窗口内构造请求。"""

    def __init__(self, current_chars: int, max_chars: int) -> None:
        self.current_chars = current_chars
        self.max_chars = max_chars
        super().__init__(
            f"Context requires {current_chars} chars but the limit is {max_chars}"
        )
