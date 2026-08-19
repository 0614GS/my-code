"""Provider profile 与已解析运行时设置共用的校验逻辑。"""

from urllib.parse import urlsplit


def validate_base_url(value: str) -> str:
    """在不发起连接的情况下校验 Anthropic 兼容 HTTP endpoint。"""

    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "base URL must be an http(s) URL without credentials, query, or fragment"
        )
    return normalized
