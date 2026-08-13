"""智能体引擎及其事件协议共用的小型值类型。"""

from dataclasses import dataclass

from nano_code.messages import TokenUsage


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """一次用户提示的终态数据。"""

    text: str
    turns: int
    usage: TokenUsage
