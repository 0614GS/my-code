# my-code 架构手册

本目录描述 my-code 当前实现。`refactor/` 保存模块化改造的原则、阶段记录和架构守卫规范；被忽略的 `claude-code/` 只用于对照行为，不是本项目的包结构来源。

## 架构地图

```text
CLI / TUI
    │
    v
ChatService ──> Session + ContextSession + ToolResultStore
    ├────────────> ContextEngine <──── AgentEngine
    │                    │                 │
    │                    ├── ContextPlanner ├── ModelClient ──> Anthropic / OpenAI Responses
    │                    └── ContextCompactor
    │
    v
ToolExecutor <──── ToolRoundExecutor <──── AgentEngine
    │
    └── Tool presentation + PermissionPolicy + Tool

Conversation facts ──codec──> Session JSONL
```

`conversation` 是内存对话事实的权威来源，`sessions` 负责持久化与恢复，`context` 负责发给模型的请求时投影与 compact proposal。活动会话由 `ChatService` 持有；`AgentEngine` 不保存会话状态，也不代理 context inspection、手动 compact 或工具历史展示。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [01-agent-loop.md](01-agent-loop.md) | 一次用户输入如何运行到终态 |
| [02-messages-and-sessions.md](02-messages-and-sessions.md) | 对话事实、持久化和恢复 |
| [03-context-management.md](03-context-management.md) | 模型上下文、预算和压缩 |
| [04-tool-system.md](04-tool-system.md) | 工具注册、权限和执行 |
| [05-permission-system.md](05-permission-system.md) | 权限规则与决策顺序 |
| [06-mcp-and-tool-discovery.md](06-mcp-and-tool-discovery.md) | 尚未实现的 MCP 边界 |
| [07-hooks.md](07-hooks.md) | 尚未实现的 Hooks 边界 |
| [08-mvp-scope.md](08-mvp-scope.md) | 当前功能范围 |
| [09-storage-and-settings.md](09-storage-and-settings.md) | 配置、凭据和本地存储 |
| [10-terminal-ui.md](10-terminal-ui.md) | TUI 与 Chat 的交互 |
| [11-prompt-management.md](11-prompt-management.md) | System prompt 与用户上下文 |
| [12-package-boundaries.md](12-package-boundaries.md) | 模块所有权、公开 API 和依赖方向 |

## 统一术语

- **Turn**：一条用户输入到正常完成或达到 step 上限。
- **Step**：一次模型响应及其可选工具轮。
- **ToolRound**：同一 AssistantMessage 中工具调用的串行执行和结果汇总。
- **Conversation**：当前进程中的 canonical facts。
- **Session**：Conversation 与 JSONL 提交边界。
- **Context**：从 Conversation 构造的临时 ModelRequest，不是第二份历史。
- **Compact**：写入摘要事实并建立新的工作集边界。
