"""按 Agent 核心职责拆分的 inbound/outbound ports。

各协议位于对应职责模块中。Agent 包根目录负责公开稳定的应用 API，内部代码
应从具体 port 模块导入，避免通过一个聚合模块隐藏依赖方向。
"""
