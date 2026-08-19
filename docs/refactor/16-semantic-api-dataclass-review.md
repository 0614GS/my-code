# 语义 API 与 dataclass 审计

## 公开 API 收敛

顶层领域包 `__init__.py` 不再汇总符号。生产代码直接从 `model.request`、`chat.events`、`permissions.models` 等语义子模块导入，目标模块以静态 `__all__` 声明能力。

Chat 删除了对 Config、Model、Permissions、Providers、Sessions、Tools 和 Features 类型的二次导出。TUI 因而显式依赖这些类型的真实所有者，但仍不访问 Agent、Context、Conversation、SessionStore、ToolExecutor 或 Provider SDK。

AST 守卫删除了“只能从包根导入”的 deep-import 检查，替换为：

- 依赖允许表与无环检查；
- 语义模块必须声明静态 `__all__`；
- 导入符号必须在目标清单内；
- 禁止私有路径、wildcard 和跨所有者 re-export；
- 顶层领域包初始化文件不得聚合 API。

## Chat 类型拆分

原 `chat/models.py` 按语义拆为：

- `chat.events`：turn outcome 和流事件；
- `chat.history`：恢复历史投影；
- `chat.status`：安全运行状态；
- `chat.permissions`：host 权限请求和 deferred prompter。

不保留旧路径兼容导出。

## dataclass 结论

生产 dataclass 从 157 个降为 151 个：

- Agent stream 直接使用 `AgentTurnSucceeded`、`AgentMaxStepsReached` 两个终态，删除两层 `result` 包装。
- Chat stream 直接使用 `TurnSucceeded`、`MaxStepsReached` 两个终态，删除相同包装。
- user、assistant、system 三种文本历史合并为 `HistoryText(role, text)`，净减少两个类型。

以下相似结构有意保留：Conversation 与 JSONL records、Model blocks 与持久化 records、Provider profile/update/connection/view、Permission 各决策阶段、Context budget 与 Chat status，以及不同阶段的 attachment。它们的可信度、生命周期或协议边界不同，合并会重新制造所有权混乱。

## 文档

`docs/README.md` 和 01–12 已改为 nano-code 当前实现手册。MCP 与 Hooks 明确记录为未实现能力；Claude Code 只作为设计对照，不再主导正文结构。
