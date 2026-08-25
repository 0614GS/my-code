# 状态所有权与生命周期

## 目标结构

```text
CLI / TUI
    |
    v
Application services
    |
    v
AppState
├── WorkspaceState
├── active Session
│   ├── persisted conversation facts
│   ├── session-scoped context entries and caches
│   ├── compaction state
│   ├── provider replay sidecars
│   └── private persistence
├── PermissionState
├── ToolState
├── TaskSupervisor
├── AgentRunFactory
├── McpRuntime
├── SkillRuntime / SkillCatalog
└── ProviderRuntime

SessionSnapshot + request input
    |
    v
stateless ContextEngine
    |
    v
ModelRequest
    |
    v
Provider adapter
```

## AppState 的职责

`AppState` 持有当前 runtime 中需要跨用例存活的状态胶囊：

```python
@dataclass(slots=True)
class AppState:
    workspace: WorkspaceState
    session: Session
    permissions: PermissionState
    provider: ProviderRuntime
```

具体字段名可以在实现阶段调整，但所有权不得改变。

`AppState` 负责：

- 原子切换活动 Session；
- 原子切换 Provider connection、client 和 active model environment；
- 协调 workspace、runtime permission 与活动 Session 的一致视图；
- 为 application service 提供稳定、可测试的状态入口；
- 在 runtime 关闭时释放 Provider client、pending operation 等资源。

`AppState` 不负责：

- 构造模型上下文；
- 执行 Agent loop；
- 执行工具；
- 解析 Provider 流；
- 暴露一个可被任意模块导入的进程全局单例。

只有 application service、CLI/TUI host 和 bootstrap 可以持有完整 `AppState`。Context、Agent、Tool 和 Provider adapter 必须通过显式参数接收最小 snapshot 或依赖，不能反向查找全局状态。

## 状态表

| 状态 | 唯一所有者 | 生命周期 | 是否持久化 |
| --- | --- | --- | --- |
| 当前 workspace 与安全边界 | `AppState.workspace` | runtime | 否；配置可单独落盘 |
| 当前活动 Session 引用 | `AppState` | runtime，切换时原子替换 | Session 自身负责 |
| 对话事实与工作集 | `Session` | session | 是 |
| compact replacement 与 boundary | `Session` | session | 是 |
| 工具展示快照与外置工具结果 | `Session` 私有持久化 | session | 是 |
| attachment delivery | `Session` 的 context state | live session | 默认否；显式用户附件事实另行持久化 |
| Todo reminder delivery | `Session` 的 context state | live session 或 request | 否；Todo 事实来自已提交工具结果 |
| session prompt/user-context cache | `Session` 的 context state | live session | 否 |
| Provider replay sidecar | `Session` | 与关联 entry/working set 一致 | 是，opaque |
| runtime permission mode/rules | `AppState.permissions` | runtime | 持久规则由 config 写入 |
| Provider connection/client/model environment | `AppState.provider` | runtime | profile/credential 由 config/auth 写入 |
| 动态工具来源目录 | `AppState.tools` | runtime；source 原子替换 | 否；来源配置可落盘 |
| task tree 与终态快照 | `AppState.tasks` | runtime | 否 |
| MCP connections、诊断与 server tool source | `AppState.mcp` | runtime | 否；server spec 来自 settings |
| Skill index、诊断与 pending activation | `AppState.skills` | runtime / per-run next step | 否；SKILL.md 是外部配置数据 |
| child Session/conversation | 对应 `AgentRun` | 单次 child task | 是，独立 JSONL |
| turn、step、usage accumulator | Agent 调用栈 | 单次 turn | 否 |
| Provider streaming parser/delta | Provider 调用栈 | 单次 step | 否 |
| tool round progress | Tool round 调用栈 | 单次 tool round | 否 |
| pending permission approval | application 调用栈 | 单次请求 | 否 |

## Session 是对话状态唯一入口

Session 对外提供不可变 snapshot 和语义提交操作：

```python
class Session:
    def snapshot(self) -> SessionSnapshot: ...

    def append_human_message(self, message: HumanMessage) -> None: ...

    def append_assistant_message(self, message: AssistantMessage) -> None: ...

    def append_tool_results(self, batch: ToolResultBatch) -> None: ...

    def commit_compaction(self, proposal: CompactionProposal) -> None: ...
```

调用方不得：

- 获取可变的 `Conversation` 后直接 append；
- 直接操作 `SessionStore`、JSONL record 或 session 文件路径；
- 直接写工具结果目录；
- 在 Context、Agent 或 Chat 中缓存另一份 Session messages；
- 通过重新读取磁盘刷新一个已打开 Session。

`Conversation` 可以暂时保留为 Session 内部的校验与 working-set 数据结构，但最终只能通过私有字段存在。其公开可变入口应被 Session API 取代。

## 持久化透明性

进程运行期间，Session 的内存状态是读取权威来源，本地存储是恢复依据。持久化实现对 Session 调用方透明，但提交必须保持强一致性：

```text
基于当前内存状态构造候选
    -> 校验 parent/tool pairing/compaction 不变量
    -> 持久化完整变更
    -> 持久化成功后原子替换内存状态
```

写入失败时：

- 内存 entries 不变；
- working set 不变；
- tool presentation/replay sidecar 索引不变；
- application service 得到明确失败；
- 不允许后台异步写入造成“调用成功但无法恢复”的状态。

具体存储可以继续使用 JSONL。`SessionStore`、codec 和 records 应成为 `sessions` 包的私有实现；catalog 可以暴露只读的 session summary，但不能暴露存储 schema。

## Session 切换

切换时先完整构造候选 Session，再替换活动引用：

```text
load + validate + repair candidate Session
    -> 初始化 candidate session context state
    -> 校验工具结果与 replay sidecar
    -> 构造 history projection
    -> AppState 原子替换 active Session
```

任何一步失败，旧 Session、Provider、Permission 和 Workspace 状态都不得被部分修改。submit、stream、compact、resume 和 provider switch 仍需共享明确的互斥边界。

## 其他模块的状态约束

### Context

- `ContextEngine`、planner、normalizer、budgeter 和 attachment projector 无跨调用可变状态。
- 原 `ContextSession` 的 delivery/cache 状态迁入 `Session` 内部的 session context state。
- runtime-stable prompt section 在 bootstrap 时解析为不可变 snapshot；不再由可变 `PromptRegistry` cache 隐式持有。
- ContextEngine 只接收 `SessionSnapshot` 和 request-scoped input，输出 `ContextPlan`。

### Agent

- `AgentEngine` 不持有 AppState 或 Session。
- turn/step 计数和 usage accumulator 只存在于一次调用。
- Agent 通过 Session 语义方法提交事实，不访问 persistence。
- 并发 child AgentRun 各自持有 Session、Agent 组件和 provider lease；不共享可写上下文状态。
- foreground Subagent 只把结构化最终结果投递给父 ToolCall；child transcript 不合并进 parent Session。

### Tasks

- `AppState.tasks` 是唯一进程内 TaskSupervisor，拥有有限状态机、父子取消树、终态快照和 runtime events。
- Task progress/result 不进入 Session canonical conversation；后续 Feature 必须显式投递结构化结果。
- runtime close 先停止接收并取消全部 task，确认终态后才回收 AgentRun/provider lease。
- background Subagent 的查询与通知记录由 SubagentController 按 root run owner 保存；Session 只保存非持久化 delivery/cursor，不把 TaskSnapshot 写成 conversation fact。

### MCP

- `AppState.mcp` 是唯一 McpRuntime，拥有 transport、连接状态和 server source 的注册/撤销。
- MCP tool 只作为标准 Tool 进入 ToolCatalog；当前 step 使用的 adapter 不因 source 后续撤销而换成新实例。
- 连接状态、诊断和 discovery cache 不进入 Session；远端 ToolCall/ToolResult 仍按普通 conversation fact 持久化。
- runtime close 在 task/run 清理后、provider client 关闭前回收全部 MCP transport。

### Skills

- `AppState.skills` 是唯一 SkillRuntime；其中的 SkillCatalog 原子替换分层索引，pending activation 按 run ID 隔离。
- discovery 只索引选择所需元数据与 fingerprint；`Skill` 标准 Tool 执行时才读取并复验正文，目录中的其他代码文件不会被导入或执行。
- 激活内容只通过下一次不可变 RunCapabilitySnapshot 进入 ModelRequest；Assistant 提交后 acknowledgement 删除对应 activation，不写入 Session context cache。
- allowed-tools 只对当前 run 的下一 step ToolCatalogSnapshot 做交集；reload 或 source 替换不修改已经捕获的 step。
- runtime close 在 run 清理后、MCP transport 关闭前撤销 Skill Tool source 并清空 pending activation。

### Tools

- application-lifetime `ToolCatalog` 只有 `AppState.tools` 一份；来源更新原子发布新版本。
- Agent step 使用不可变 `ToolCatalogSnapshot`，请求 definitions 与 ToolRound 执行版本一致。
- `ToolExecutor` 不绑定活动 Session 或活动工具结果目录。
- Tool 返回领域结果；由 Session 提交路径负责结果外置、展示快照和持久化。

### Permissions

- runtime mode/rules 只有 `AppState.permissions` 一份。
- ToolExecutor 每次执行接收 permission snapshot 或专用决策服务。
- 持久化更新成功后才替换 runtime permission state。

### Providers

- `ProviderRuntime` 持有前台 connection/client、capabilities、切换锁和 ProviderLeaseRegistry。
- 每个 AgentRun lease 捕获创建时的 connection 并拥有独立 client/stream lock；provider switch 只影响新 lease。
- Provider adapter 只消费 `ModelRequest`，不读取 AppState 或 Session。
- Provider continuation 是 Session 的 opaque replay sidecar，不是 ProviderRuntime 的对话历史。

## 防止 AppState 退化为 god object

- 新状态加入 `AppState` 前必须写明唯一所有者、生命周期、持久化和失效条件。
- request/turn/step 局部状态禁止提升到 `AppState`。
- 派生数据优先从 snapshot 投影，禁止为了读取方便建立第二份可写缓存。
- 模块不得通过 `get_app_state()` 等全局函数隐藏依赖。
- AppState 的字段是有边界的状态胶囊；跨胶囊变更只能由明确 application use case 协调。
