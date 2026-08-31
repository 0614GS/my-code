# 当前状态评估

本文记录生命周期与命名整理完成后的源码证据。规范定义见
[../11-lifecycle-and-naming.md](../11-lifecycle-and-naming.md)，稳定实现事实已同步到正式架构专题。

## 当前所有权图

```text
ApplicationService                         use-case façade
└── ApplicationRuntime                     process owner
    ├── Workspace / PermissionRuntime / ToolCatalog
    ├── TaskSupervisor / AgentRunFactory / ProviderRuntime
    ├── McpRuntime / SkillRuntime / observer shutdown
    ├── foreground close entry
    └── ActiveSessionBinding
        ├── Session
        ├── SessionContextCache
        └── effective PermissionPolicy

AgentRun
├── independent Run ID
├── typed child Session + parent Session/Run lineage
├── SessionContextCache / Agent components
└── Provider lease
```

源码证据：`runtime/application.py` 定义唯一 application owner 与原子 foreground
binding；`bootstrap.py` 只用私有 `_BootstrapComponents` 连接 façade 所需窄组件，不再
重复列举 runtime 资源；`runtime/runs.py` 拥有 child run capsule 与 lease 释放。

## Session 集合与身份

磁盘 catalog、当前 foreground binding 和 live child run registry 保持三个独立集合。
v7 `SessionStart` 持久化受控 `SessionKind`、parent Session、created-by Run 和 agent
name。Catalog 只返回 foreground；v6 缺少 kind 时按已决兼容策略解释为 foreground，
未知 kind 失败关闭。child 创建时分别生成 Session ID 与 Run ID。

源码证据：`sessions/models.py`、`sessions/_codec.py`、`sessions/catalog.py`、
`features/subagents/controller.py`；回归位于 `tests/unit/sessions/test_session_store.py`
和 `tests/integration/test_subagent_lifecycle.py`。

## Session switch

启动、resume 与 fresh-context plan handoff 都形成新的 Session activation。resume 在
operation lock 内完整恢复候选后构造并发布 `ActiveSessionBinding`；加载失败或取消不
修改旧 binding。pending input 在发布前校验并重绑。Question 从每次
`ToolExecutionContext` 判断 current/root Session，不捕获启动 Session；后台 Bash 也从
execution identity 动态选择 Session 输出目录。旧 Session 的后台 registry ownership 不会
转移。

源码证据：`application/service.py`、`runtime/application.py`、
`application/turns/questions.py`、`features/background_tasks/bash.py`；失败、取消、
Question 和输出目录回归分别位于 application、plan mode 与 background task 测试。

## Run、Invocation 与 Turn

Session 是 durable identity，`Session.run_id` 是每次进程 activation identity；显式
`AgentRun` 会在执行前绑定自己的 Run ID。覆盖完整 Agent loop 的 v7 journal 使用
`InvocationStarted/InvocationFinished` 与 `invocation_id`；v6
`turn_started/turn_finished` 只在 codec 内兼容映射。Conversation Turn 仍由每条已接受
`HumanMessage` 的 causal boundary 推导，step-boundary steering 不把 Invocation 误称为
单个 Turn。

## 命名审计

生产代码已迁移为 `ApplicationRuntime`、`ActiveSessionBinding`、
`SessionContextCache`、`ContextPlanningInput`、`AttachmentProjectionInput`、
`ToolExecutionContext`、`ApplicationStatus`、`ContextUsageView`、
`ProviderContinuation` 和 `SessionOperations`。`WorkspaceState`、`ToolState`
与旧名称 re-export 均不存在。保留的 `PickerState`、`SlashMenuState`、
`QueueInputState` 和 `McpConnectionState` 都有明确 owner 与更新边界。
