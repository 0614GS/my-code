"""Provider 无关的未完成模型流有界重试策略。"""

import random

MAX_STREAM_RETRIES = 2
_BASE_DELAY_SECONDS = 0.5


def stream_retry_delay(retry_number: int) -> float:
    """使用有界抖动，避免并发请求在端点恢复时同步重放。"""

    if retry_number < 1:
        raise ValueError("retry_number must be positive")
    base = _BASE_DELAY_SECONDS * (2 ** (retry_number - 1))
    return base * (0.75 + random.random() * 0.5)


__all__ = ["MAX_STREAM_RETRIES", "stream_retry_delay"]
