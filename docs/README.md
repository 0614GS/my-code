# my-code 架构手册

这里描述的是 my-code 的当前实现。文档按“先理解一次请求如何运行，再按专题深入”的顺序组织；精确 API、配置 schema 和依赖允许表始终以代码与测试为准。

## 推荐阅读路径

1. [总体架构](00-architecture.md)：先认识运行时组件、所有权和唯一数据流。
2. [Runtime 与 Agent loop](01-runtime-and-agent-loop.md)：跟随一次用户输入走完 turn、step、工具与取消路径。
3. [状态、对话与 Session](02-state-conversation-sessions.md)：理解哪些数据是事实、如何提交和恢复。
4. [Context 与模型输入](03-context-and-model-input.md)：理解事实如何变成 provider-neutral 请求。
5. [工具、权限与工作区](04-tools-permissions-workspace.md)：理解工具发现、执行和安全边界。
6. [扩展能力](05-extensions.md)：理解 MCP、Skill、Subagent、后台任务和 Hooks 的位置。
7. [生命周期与命名体系](11-lifecycle-and-naming.md)：统一 State、Context、Runtime、Session、Run 与 Invocation 等术语。

按需查阅：

- [Provider、配置与存储](06-providers-settings-storage.md)
- [终端 UI](07-terminal-ui.md)
- [包边界与架构守卫](08-package-boundaries.md)
- [当前能力范围](09-current-scope.md)
- [可观测性与评测](10-observability-and-evaluation.md)
- [架构整理任务索引](refactor/todolist.md)

## 架构地图

```text
CLI / TUI
    -> ApplicationService
        -> ApplicationRuntime
            ├── ActiveSessionBinding
            ├── Workspace / PermissionRuntime
            ├── ToolCatalog / TaskSupervisor
            ├── AgentRunFactory / ProviderRuntime
            └── McpRuntime / SkillRuntime

ApplicationService
    ├── turns -> AgentEngine
    ├── sessions -> Session operations / projections
    ├── configuration -> Provider / mode operations
    └── activity -> background / Subagent views

AgentEngine
    -> ContextEngine -> provider-neutral ModelRequest -> ModelClient
    -> ToolRoundExecutor -> ToolExecutor -> standard Tool
```

`ApplicationRuntime` 是活动 runtime 状态的唯一入口，但不是全局变量或 service locator。`Session` 是对话事实及其持久化的唯一公开边界；`ContextEngine` 只构造单次模型请求。内置工具、MCP、Skill、Subagent 和后台任务最终都复用标准 Tool、权限和 Session 提交路径。

## 统一术语

项目术语的规范定义统一维护在[生命周期与命名体系](11-lifecycle-and-naming.md)。专题文档可以解释领域内行为，但不应为 State、Context、Runtime、Session、Run、Invocation、Turn、Step、Request 或 Call 建立另一套定义。生命周期与命名迁移的决策记录和完成证据位于[架构整理任务索引](refactor/todolist.md)。

## 文档维护原则

- 一项事实只在一个专题中完整解释，其他位置链接到它。
- 当前能力用现在时描述；已完成迁移、阶段统计和测试数量由 Git 历史保存。
- 不复制机器可读的依赖允许表、配置 schema 或测试矩阵。
- 改动架构边界时同时更新对应专题；有意偏离参考实现的地方记录在[当前能力范围](09-current-scope.md)。
