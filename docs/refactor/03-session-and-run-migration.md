# Session、Run 与 Invocation 迁移

本文整理持久化 Session、live Run、Agent Invocation 和 Conversation Turn 的迁移决策点。

## Session 分类

目标 Session 元数据至少能够表达：

```text
session_id
session_kind             foreground | subagent | 其他明确产品类型
parent_session_id?       durable conversation ownership
created_by_run_id?       可选的执行来源
agent_name?              可选的产品展示身份
```

`session_kind` 必须是受控枚举，不能根据 title、首条 prompt、文件路径或是否存在 `parent_run_id` 猜测。`/resume` 默认只列出产品允许激活为 foreground 的 kind；child transcript 仍可通过 Subagent activity/transcript 用例查看。

### 已定策略

- plan handoff 是新的 foreground Session，不建立独立 kind；
- v6 transcript 缺少 kind 时兼容视为 foreground，v5 及更早仍拒绝；
- v7 同时增加 Session identity 字段并把 journal 改为 Invocation 名称；
- catalog 与 TUI `/resume` 只允许 foreground，不提供恢复 child 的公共入口。

## Foreground Session 切换

启动、`/resume` 和 fresh-context plan handoff 应共用候选构造入口。迁移需要覆盖：

- Session 与 SessionContextCache 配对；
- permission mode/rules/confirmation 的恢复来源；
- pending input 必须为空或有明确迁移策略；
- Question root identity 从当前 binding 获取；
- Bash/background 输出路径按 execution owner 动态解析；
- Subagent、TaskList、TaskCancel 从 ToolExecutionContext 获取 lineage；
- activity view 和 notification source 按当前 foreground Session 过滤；
- switch 后旧 Session 的后台任务仍保持原 owner。

不要为每个工具增加独立 `rebind_session()`。优先让 session identity 从 Step 捕获的 ToolExecutionContext 或 ActiveSessionBinding 派生。

## Session 与 Run 身份

目标关系是：

```text
Session ID    durable conversation identity
Run ID        one live activation identity
Invocation ID one Agent loop execution identity
Turn boundary one accepted HumanMessage boundary
Request ID    one audited ModelRequest identity
Attempt       one request delivery attempt
```

Run ID 不再默认等于 Session ID。parent lineage 分别说明 parent Session 与 parent Run，避免一个 `parent_run_id` 同时承担持久化关系和进程内任务树关系。

### Foreground Run 方案

采用方案 3：foreground 不建立拥有 provider lease 的 `AgentRun`，但每次 Session
activation 生成独立 Run ID，并由 `ActiveSessionBinding` 与 Session、Cache 和权限策略
一起发布。child 继续使用 `AgentRun` capsule；两条路径共享相同的 Session/Run 身份语义。

## Invocation journal 迁移

一次 instrumented stream 只产生一组 `InvocationStarted/InvocationFinished`，但 interactive stream 可以接受多条用户 steering，因此 record 的行为语义是 Invocation。

- record/DTO/observability 已迁移为 `InvocationStarted/Finished`；
- Conversation Turn 继续由 HumanMessage causal boundary 推导；
- 如果未来需要持久化 Turn metadata，建立独立 record，不能复用 Invocation terminal；
- v6 journal 在恢复时映射为 legacy invocation，不改写 transcript 猜测额外 Turn。

## 兼容与原子性

- codec 必须精确校验新增字段和枚举；未知 kind 失败关闭。
- catalog 读取 header 时即可确定是否展示，不扫描完整 child transcript 推断类型。
- schema 迁移不能丢失 request audit、tool pairing、compact boundary 或 permission mode。
- resume 候选完全构造后才发布；失败、取消和 I/O 错误保留旧 ActiveSessionBinding。
- child Run terminal 后释放 Provider lease；Session transcript 的保留不应依赖 live Run 对象继续存在。
