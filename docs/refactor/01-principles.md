# 重构原则

## 目标

1. 明确每个类、值对象和运行状态的所有者。
2. 让模块通过公开 API 明确声明能力。
3. 建立简单、单向、无环的模块依赖图。
4. 删除没有实际替换需求的 port、adapter 和转发层。
5. 用自动检查阻止边界重新退化。

## 非目标

- 不要求套用 Clean Architecture、DDD 或六边形架构模板。
- 不要求所有外部调用都有一层自定义接口。
- 不为了测试 mock 而创建 Protocol。
- 不在本轮顺带改变 Agent、权限、会话或上下文的产品行为。
- 不提前创建尚未实现的 Hooks、MCP、Plan Mode 或 Subagent 结构。

## 所有权规则

一个类型放在哪里，由它维护的不变量和生命周期决定，而不是由 import 数量决定。

- 已发生且可恢复的对话事实属于 `conversation`。
- 发给模型或由模型产生的 provider-neutral 数据属于 `model`。
- 一次 Agent turn 的循环状态和终止语义属于 `agent`。
- JSONL schema、兼容解析和 session catalog 属于 `sessions`。
- 模型请求中的临时内容、预算和 attachment delivery 属于 `context`。
- 工具定义、注册和受控执行属于 `tools`。
- 用户可感知且跨多个基础模块的纵向能力属于独立 `features.<name>`。
- 颜色、组件、按键和布局等 host 细节属于 `tui` 或 `cli`。

结构相似不代表语义相同。只有当可信度、生命周期或协议边界确实不同，才创建两个 DTO，并在边界处显式转换。

类型所有权与存储位置不是一回事。`sessions` 负责序列化 `ConversationMessage`，不因此拥有消息语义；`context` 从 Conversation 构造 `ModelRequest`，不因此拥有对话历史。

所有可变状态都必须说明创建、更新、恢复和销毁时机。无法说明生命周期的状态不得放进长生命周期 service 或全局容器。

## 抽象准入

默认依赖具体类。构造函数注入具体对象仍然便于测试，不需要先定义接口。

Protocol 只在以下情况之一成立时引入：

- 已有两个生产实现，例如 OpenAI 与 Anthropic 的 `ModelClient`；
- 它是确认存在的插件或扩展点；
- 它隔离的是必须由调用方提供的 callback 能力。

以下理由不足以引入 Protocol：

- 单元测试需要 fake；
- 希望遵循依赖倒置；
- 未来可能出现第二种实现；
- 只想隐藏一个已有具体类。

重构后应删除 `AgentInboundPort`、`ContextPort`、`ToolRoundPort`、`CompactionPort`、`SessionRepository` 等只有一个实现且只做转发的抽象。若未来出现第二个真实实现，再从实际共同接口中提取 Protocol。

## 文件组织

- 优先使用表达语义的文件名，例如 `events.py`、`usage.py`、`rules.py`。
- 避免含混的 `types.py`、`utils.py`、`common.py`。
- `service.py` 和 `manager.py` 必须对应一个清楚的应用能力，不能成为杂物入口。
- 不为尚未存在的实现预建空目录。
- 顶层领域包的 `__init__.py` 不聚合 API；公开能力由语义子模块的静态 `__all__` 声明。
- 公开模块不执行注册、读取配置或创建对象，也不转发其他所有者的符号。
