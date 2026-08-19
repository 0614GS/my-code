# my-code 架构手册

本目录描述 my-code 当前实现。`refactor/` 与 `state-management/` 保存已完成改造的决策、迁移记录和验收证据；被忽略的 `claude-code/` 只用于对照行为，不是本项目的包结构来源。

## 架构地图

```text
CLI / TUI
    │
    v
ChatService ──> AppState
                   ├── WorkspaceState
                   ├── PermissionState ──> PermissionPolicy
                   ├── active Session
                   │      ├── canonical conversation + working set
                   │      ├── context delivery/cache + replay sidecar
                   │      └── private JSONL + externalized tool results
                   └── ProviderRuntime ──> ProviderRouter

ChatService ──> AgentEngine ──> ContextEngine ──> provider-neutral ModelRequest
                        ├─────> ModelClient ──> Anthropic / OpenAI Responses
                        └─────> ToolRoundExecutor ──> ToolExecutor
```

`AppState` 是活动 runtime 状态的唯一入口，但不是全局变量或 service locator。`Session` 是对话事实及其持久化的唯一所有者；`context` 只生成请求投影与 compact proposal。`ChatService` 协调用例，不复制 session、provider 或 permission 状态；Agent、Context、Tool 和 Provider adapter 均不持有完整 AppState。

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

状态所有权的已实现设计与验收证据见 [state-management/README.md](state-management/README.md)。

## 统一术语

- **Turn**：从一次真实用户输入开始，到下一次真实用户输入之前；可以包含多个 step。
- **Step**：一次模型调用；只有完整响应才提交一条 AssistantMessage。
- **ToolRound**：同一 AssistantMessage 中工具调用的串行执行和结果汇总。
- **Conversation facts**：Session 私有持有的 provider-neutral canonical entries。
- **Session**：内存事实、工作集、session context state 与透明持久化边界。
- **Context**：从不可变 Session snapshot 构造的临时 ModelRequest，不是第二份历史。
- **Compact**：写入摘要事实并建立新的工作集边界。
