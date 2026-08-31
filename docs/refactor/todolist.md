# 生命周期与命名整理任务索引

本文只维护任务状态和最终决策链接，不重复已经进入正式架构文档的设计细节。

状态约定：`[ ]` 未开始，`[-]` 进行中，`[x]` 已完成。只有实现、目标测试、完整质量门和对应文档全部完成后才能标记完成。

## 最终文档

| 文档 | 负责内容 |
| --- | --- |
| [01-current-assessment.md](01-current-assessment.md) | 完成后的所有权、生命周期和命名证据 |
| [03-session-and-run-migration.md](03-session-and-run-migration.md) | Session 分类、resume、Run 身份和 Invocation 兼容决策 |
| [../11-lifecycle-and-naming.md](../11-lifecycle-and-naming.md) | 生命周期与命名规范词汇 |
| [../00-architecture.md](../00-architecture.md) | ApplicationRuntime 与资源所有权 |
| [../02-state-conversation-sessions.md](../02-state-conversation-sessions.md) | Session 持久化、切换与恢复 |
| [../08-package-boundaries.md](../08-package-boundaries.md) | 包边界和依赖方向 |
| [../10-observability-and-evaluation.md](../10-observability-and-evaluation.md) | Invocation journal 与可观测语义 |

## 最终决策

- foreground 与 subagent 使用持久化 `SessionKind` 区分；plan handoff 仍是 foreground。
- transcript schema v7 持久化 Session lineage，并使用 Invocation journal；v6 缺失 kind 时兼容视为 foreground，v5 及更早拒绝，未知 kind 失败关闭。
- 每次 foreground activation 生成独立 Run ID，并随 `ActiveSessionBinding` 原子发布；foreground 不建立拥有 provider lease 的 `AgentRun`，child 继续使用 `AgentRun` capsule。
- catalog 和 `/resume` 只返回 foreground Session；child Session ID 与 live Run ID 不复用。
- Conversation Turn 由已接受的 `HumanMessage` causal boundary 定义；覆盖完整 Agent loop 的持久化与可观测终态使用 Invocation 语义。

## 完成清单

### 0. 固定术语和现状

- [x] 发布 State、Snapshot、View、Context、Environment、Cache、Runtime、Session、Run、Invocation、Turn、Step、Request、Call、Attempt、Execution 和 Task 的统一定义。
- [x] 为现状诊断中的每项问题补充或确认源码证据，移除已经失效的条目。
- [x] 决定 Session kind、Run identity 和旧 transcript 兼容策略中的开放问题。

### 1. 建立 Session 身份与目录边界

- [x] 为 foreground、subagent 和其他产品会话定义持久化 Session kind。
- [x] 定义并持久化 parent Session/Run 关系，避免用 UUID 相等隐式表达所有权。
- [x] 使 Session catalog 默认只返回允许 `/resume` 的 foreground Session。
- [x] 明确旧 schema 的升级、拒绝或兼容读取策略。
- [x] 为 child transcript 不进入 `/resume` 增加回归测试。

### 2. 引入原子的 ActiveSessionBinding

- [x] 引入活动前台会话绑定，包含 Session、Run ID、SessionContextCache 和 effective permission policy。
- [x] 审计 permission、pending input、Question、后台任务路径和 activity 中必须随 resume 更新的状态。
- [x] 将启动、`/resume` 和 plan handoff 统一为“构造候选、完整验证、原子发布”流程。
- [x] 修复 Question 在 resume 后仍绑定启动 Session 的问题。
- [x] 修复后台 Bash 在 resume 后仍使用启动 Session 输出目录的问题。
- [x] 覆盖失败和取消时旧 binding 完整保留的测试。

### 3. 收拢进程级 Runtime 所有权

- [x] 引入真正的 `ApplicationRuntime`，明确 start、close、operation lock 和资源关闭顺序。
- [x] 决定 foreground Agent、ContextEngine、ToolExecutor、background registry、Subagent controller 和 observer 的唯一 owner。
- [x] 消除 `ApplicationAssembly`、`ApplicationRuntime`、`ApplicationService` 对同一资源的重复表达。
- [x] 保持 `ApplicationService` 为 use-case façade，不让 Runtime 退化为任意服务查询容器。
- [x] 更新 Tach 和 Application 边界守卫。

### 4. 分离 Session、Run、Invocation 与 Turn

- [x] 决定 foreground Run 模型：独立 activation Run ID，不建立 provider-lease `AgentRun` capsule。
- [x] 使 Run ID 表达 live activation，不再默认等于 Session ID。
- [x] 将覆盖整个 Agent loop 的 Turn journal/observability 迁移为 Invocation 语义。
- [x] 明确 step-boundary steering 下 Turn 的 canonical 边界和展示聚合规则。
- [x] 保持 request audit、causal head 和旧 journal 的恢复兼容性。

### 5. 执行名称迁移

- [x] 迁移 `ApplicationRuntime`、`SessionContextCache` 和误用的 `*State` 包装类型。
- [x] 区分 Snapshot、View 与 Status DTO。
- [x] 将操作输入统一为带领域前缀的 `*Context` 或 `*Input`。
- [x] 将不拥有完整生命周期的 session façade 命名为 `SessionOperations`。
- [x] 更新源码、测试、文档、`__all__` 和架构守卫，不保留兼容性 re-export。

### 6. 完成验证与文档收口

- [x] 按风险分阶段运行目标测试。
- [x] 运行 `uv run python scripts/check.py` 完整质量门。
- [x] 更新 `docs/00` 至 `docs/10` 中受影响的当前实现描述。
- [x] 在 `docs/09-current-scope.md` 记录有意保留的差异。
- [x] 删除已经完成且不再需要的临时迁移说明；本索引只保留最终决策链接。

## 全局完成条件

- [x] 任何 live resource 都有唯一 owner、创建点和关闭点。
- [x] 任一 Session switch 都替换完整 session-bound bundle，不遗留启动 Session 身份。
- [x] foreground 与 child Session 可以被目录和恢复策略可靠区分。
- [x] State、Context、Runtime、Cache、Snapshot、View 和 Status 的生产类型符合统一定义。
- [x] Session、Run、Invocation、Turn、Step、Call 和 Attempt 的 ID 与 journal 语义不再混用。
- [x] 目标测试、架构守卫和完整本地质量门通过。
