"""Provider SDK 异常到核心运行时错误的稳定映射。"""


class ModelContextOverflow(RuntimeError):
    """Provider 拒绝了超过模型上下文窗口的请求。"""
