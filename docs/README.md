# my-code 架构手册

这里描述的是 my-code 的当前实现。文档按“先理解一次请求如何运行，再按专题深入”的顺序组织；精确 API、配置 schema 和依赖允许表始终以代码与测试为准。

## 推荐阅读路径

1. [总体架构](00-architecture.md)：先认识运行时组件、所有权和唯一数据流。
2. [Runtime 与 Agent loop](01-runtime-and-agent-loop.md)：跟随一次用户输入走完 turn、step、工具与取消路径。
3. [状态、对话与 Session](02-state-conversation-sessions.md)：理解哪些数据是事实、如何提交和恢复。
4. [Context 与模型输入](03-context-and-model-input.md)：理解事实如何变成 provider-neutral 请求。
5. [工具、权限与工作区](04-tools-permissions-workspace.md)：理解工具发现、执行和安全边界。
6. [扩展能力](05-extensions.md)：理解 MCP、Skill、Subagent、后台任务和 Hooks 的位置。

按需查阅：

- [Provider、配置与存储](06-providers-settings-storage.md)
- [终端 UI](07-terminal-ui.md)
- [包边界与架构守卫](08-package-boundaries.md)
- [当前能力范围](09-current-scope.md)

## 架构地图

```text
CLI / TUI
    -> ChatService
        -> AppState
            ├── active Session + ContextRuntime
            ├── WorkspaceState / PermissionState
            ├── ToolState / TaskSupervisor
            ├── AgentRunFactory / ProviderRuntime
            └── McpRuntime / SkillRuntime

ChatService
    -> AgentEngine
        -> ContextEngine -> provider-neutral ModelRequest -> ModelClient
        -> ToolRoundExecutor -> ToolExecutor -> standard Tool
```

`AppState` 是活动 runtime 状态的唯一入口，但不是全局变量或 service locator。`Session` 是对话事实及其持久化的唯一公开边界；`ContextEngine` 只构造单次模型请求。内置工具、MCP、Skill、Subagent 和后台任务最终都复用标准 Tool、权限和 Session 提交路径。

## 统一术语

- **Turn**：从一次真实用户输入开始，到下一次真实用户输入之前；可以包含多个 step。
- **Step**：一次模型调用；只有完整响应才提交一条 `AssistantMessage`。
- **ToolRound**：同一 `AssistantMessage` 中工具调用的分组执行与有序结果闭合。
- **Conversation facts**：Session 私有持有的 provider-neutral canonical entries。
- **Context**：从不可变 Session snapshot 构造的临时模型输入，不是第二份历史。
- **Compact**：提交摘要事实并建立新的工作集边界，不删除完整 transcript。
- **Task**：由 `TaskSupervisor` 管理的进程内异步生命周期；进度不是 Conversation fact。
- **AgentRun**：独立 Session、Agent 组件和 provider lease 构成的可关闭运行单元。
- **Tool source**：以稳定来源 ID 原子发布到 `ToolCatalog` 的一组标准 Tool。

## 文档维护原则

- 一项事实只在一个专题中完整解释，其他位置链接到它。
- 当前能力用现在时描述；已完成迁移、阶段统计和测试数量由 Git 历史保存。
- 不复制机器可读的依赖允许表、配置 schema 或测试矩阵。
- 改动架构边界时同时更新对应专题；有意偏离参考实现的地方记录在[当前能力范围](09-current-scope.md)。
