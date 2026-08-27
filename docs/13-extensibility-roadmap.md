# 可扩展运行时架构路线图

> 状态：已完成，M0–M6 已于 2026-08-24 通过验收。本文同时保留目标架构、实施闸门和逐阶段验证证据；当前实现事实仍以 [README.md](README.md)、[08-mvp-scope.md](08-mvp-scope.md) 和代码为准。

## 1. 目标与完成标准

本路线图服务于五项扩展能力：

1. 同一 ToolRound 内安全的工具并行调用。
2. MCP server 注册、连接和工具发现。
3. Skill 发现、校验、加载和按需激活。
4. 可限制权限和工具范围的 Subagent。
5. 不阻塞主 Agent 的后台任务。

目标不是把五项能力分别接入现有循环，而是先建立它们共同需要的动态能力快照、运行实例和任务生命周期边界。全部完成时必须同时满足：

- 新工具来源只接入 `ToolCatalog`，不修改 Agent loop、Context planner 或 Tool executor 的分支逻辑。
- 一次模型 step 看到的工具定义与随后可执行的工具来自同一个不可变快照。
- 主 Agent、Subagent 和后台任务不共享可写 Session、Context 临时状态或 provider stream。
- 所有工具，包括 MCP 工具和启动 Subagent 的工具，都经过同一 `ToolExecutor` 权限与取消流水线。
- 同一 Session 仍只有一个前台写入者；后台 Agent 使用独立 Session。
- 每个异步操作都有唯一 ID、有限状态机、取消路径和可观察终态，不遗留无人回收的 `asyncio.Task`。
- 架构依赖图无环，`TEMPORARY_DEPENDENCY_VIOLATIONS`、`TEMPORARY_TECHNICAL_LEAKS` 和 `TEMPORARY_CYCLIC_COMPONENTS` 保持为空。
- 本文第 9 节的需求全部有自动化测试，且第 10 节质量门禁全部通过。

## 2. 启动路线图时的基线与扩展阻力

| 当前事实 | 对后续能力的影响 | 路线图中的处理 |
| --- | --- | --- |
| `ToolRegistry` 在组装时固定，`ContextPlanner` 持有固定 definitions | MCP/Skill 新增工具后，请求定义和执行 registry 容易失配 | M1 引入带版本的不可变 `ToolCatalogSnapshot` |
| `ToolRoundExecutor` 串行执行全部调用 | 独立 Read/Glob/Grep 不能并发 | M2 引入有屏障的并行调度 |
| `Tool.concurrency_safe` 只看工具类型 | 同一工具的不同输入无法表达不同风险 | M2 改为基于 invocation 输入判断 |
| `AppState` 只有一个活动 Session 和 operation lock | 不能把同一 Session 直接交给后台 Agent | M3/M4 引入独立 run、独立 child Session 和任务树 |
| `ProviderRouter` 在完整 stream 期间持有单锁 | 共用 router 会让主 Agent 与 Subagent 互相阻塞 | M3 引入每个 run 独立的 provider lease |
| `_assemble_agent()` 返回长 tuple | 每增加一个 runtime 服务都会放大 bootstrap 耦合 | M0 改为有名字的组装结果，仍由 bootstrap 唯一装配 |
| MCP、Skill 和任务没有运行期所有者 | 容易把连接、缓存或 Task 塞进 Session/全局变量 | M1/M3 明确应用生命周期所有权与关闭顺序 |

## 3. 已确定的架构决策

这些是本路线图的默认决策。实施中如需改变，先写 ADR 并同步修改本文和架构守卫。

### D1：动态能力只在 step 边界生效

每个 step 开始前捕获一次 `ToolCatalogSnapshot`，其中包含工具实例、模型 definitions、来源及版本。Skill 正文和列表通过 Conversation Attachment 进入模型输入，不建立第二份 capability contribution。

provider binding/model environment 由对应 AgentRun 捕获；Subagent 的 permission/tool 收窄在创建 child-local catalog/policy 时完成，不重复塞入 step 快照。

同一 step 的 `ModelRequest` 和 ToolRound 必须使用同一快照。MCP 重连、Skill reload 或配置变更只能影响下一个 step，不能改变正在执行的 ToolRound。

### D2：注册、选择与执行分离

- `ToolCatalog` 负责工具来源的原子注册、撤销、冲突检查和快照。
- Context 只把快照中的 definitions 投影进 `ModelRequest`。
- `ToolExecutor` 仍是参数校验、权限、hook、执行、错误归一化和取消闭合的唯一入口。
- MCP/Skill/Subagent 只提供标准 `Tool` 或运行描述，不旁路执行器。

### D3：同一 Session 不并发写入

后台任务不会解除同一 Session 的单写者约束。Subagent 无论前台还是后台都创建 child Session；父 Session 只接收结构化结果或任务通知。主 Agent 可以继续后续 step，但不会与另一个前台 turn 同时修改 canonical conversation。

### D4：Subagent 只能收窄权限

child run 的权限是“父权限 ∩ SubagentSpec 限制”，工具集是 spawn 时快照的 allowlist 子集。child 不能切换到更宽松模式、获得父级未拥有的工具、继承凭据明文或绕过 workspace 边界。

### D5：后台任务首版为进程内能力

首版后台任务不承诺进程重启后继续运行。正常关闭时必须取消并等待全部任务进入终态；异常恢复时不得把旧的 `running` 任务伪装成仍在运行。持久化/远端任务属于后续独立里程碑。

### D6：并行不改变模型可见顺序

只并发执行连续且安全的调用组；不安全调用是前后屏障。`ToolResultBatch` 始终按原始 ToolCall 顺序排列，而不是按完成时间排列。

### D7：Skill 是数据，不是可执行插件

首版 Skill 是经过 schema 校验的元数据和 Markdown 指令。Loader 不导入 Skill 目录中的 Python，不执行 shell，也不把 frontmatter 中的任意字段解释为代码。

## 4. 目标运行时结构

```text
bootstrap (唯一组装入口)
    │
    ├── ToolCatalog <── builtin / feature / MCP tool sources
    ├── SkillCatalog <── builtin / user / project / MCP skill sources
    ├── SkillRuntime ──> discovery + lazy activation + Skill tool source
    ├── McpRuntime ──> connections + discovery lifecycle
    ├── TaskSupervisor ──> task tree + result/notification store
    └── AgentRunFactory ──> one run capsule
                               ├── own Session
                               ├── own ModelClient lease
                               ├── immutable ToolCatalogSnapshot
                               ├── AgentEngine + ContextEngine
                               └── ToolRoundExecutor + cancellation scope

ChatService ──> foreground AgentRun
Agent Tool ──> SubagentController ──> foreground child run
                                  └── background TaskHandle ──> child run
```

这里的 “own” 表示可写状态不共享；Workspace、只读配置和不可变能力快照可以安全共享。`AgentEngine` 继续只负责 turn 状态机，不负责发现 MCP、扫描 Skill 或维护任务列表。

### 4.1 目标模块所有权

| 模块 | 新增所有权 | 明确不拥有 |
| --- | --- | --- |
| `tools.catalog` | `ToolCatalog`、`ToolCatalogSnapshot`、source metadata、版本与冲突 | MCP 连接、Skill 扫描、任务状态 |
| `tools.round_executor` | 安全组调度、并发上限、结果排序与轮次取消 | 后台任务生命周期 |
| `tasks` | `TaskSupervisor`、`TaskHandle`、`TaskSnapshot`、状态转换、任务树与通知 | Agent/Session 的业务实现 |
| `mcp` | server 配置解析后的运行态、transport/client 生命周期、能力发现和 `Tool` adapter | 工具授权与执行 |
| `skills` | 搜索路径、优先级、frontmatter 解析、校验、索引、按需加载 | 任意代码执行、Agent loop |
| `application` | `ApplicationRuntime`、`AgentRunFactory`、provider lease 与有序关闭 | canonical conversation |
| `features.subagents` | `SubagentSpec`、spawn/resume/cancel/output 的产品行为和 Tool adapter | 通用任务调度、provider SDK |
| `sessions` | 每个 parent/child run 自己的 canonical facts 与恢复 | MCP 连接、Skill catalog、全局 task registry |

`tasks` 是通用异步生命周期层：它接收由上层提供的 awaitable factory，不导入 `agent`、`features.subagents` 或 provider SDK。`features.subagents` 把 child run 提交给它，避免任务层反向依赖产品能力。

### 4.2 状态与生命周期

| 状态 | 所有者 | 生命周期 | 是否进入 canonical conversation |
| --- | --- | --- | --- |
| Tool source 注册表 | `ToolCatalog` | Application | 否 |
| `ToolCatalogSnapshot` | Agent run/step | Step，不可变 | 否 |
| MCP connection/client | `McpRuntime` | Application/server connection | 否 |
| Skill index/cache | `SkillCatalog` | Application，可原子 reload | 否 |
| Skill activation/listing | Parent Session Conversation | session / context entries | 正文持久化，listing 仅内存 |
| foreground parent Session | `AppState` | User session | 是，唯一权威 |
| child Session | child `AgentRun` | Subagent task | 是，仅属于 child |
| task state/result | `TaskSupervisor` | Application，首版不跨进程 | 否 |
| task completion attachment | parent Session Conversation | Parent session | 是，按 task ID 幂等 |
| Subagent 最终结果 | child Session + `TaskSnapshot` | Task | 只有作为 ToolResult/attachment 交付后才对父上下文可见 |

后台任务完成时，`TaskSupervisor` 保存唯一终态。`BackgroundTaskNotificationSource` 在父 Agent 的下一个安全 step 边界生成 durable Attachment；如果当前 turn 已结束，进程内的无 payload revision signal 会唤醒交互式 host，由 host 启动不追加 `HumanMessage` 的 continuation。Bash 输出由 attachment 给出路径并通过 Read 查询；通知进度、wake revision 和 stream delta 不进入 Conversation。

### 4.3 依赖方向

目标依赖保持单向：

```text
foundation
        ↓
model / conversation / workspace
        ↓
permissions / prompts / auth / config
        ↓
context / tools / sessions / providers / tasks
        ↓
agent / mcp / skills
        ↓
application / features.subagents
        ↓
chat
        ↓
cli / tui
        ↓
bootstrap
```

这是所有权方向，不是要求低层模块互相全部可见。具体允许边必须按实际最小依赖加入 `tests/architecture/dependency_rules.py`：

- `tools` 不得依赖 `mcp`、`skills`、`tasks` 或 `features.*`。
- `tasks` 不得依赖 `agent`、`application`、`sessions`、`providers` 或 `features.*`。
- `mcp` 可以依赖 provider-neutral `model`、`tools`、`config`，不得让 MCP SDK 类型离开 `mcp`。
- `skills` 可以依赖 provider-neutral `conversation`/`context`、`model`、`permissions` 和 `tools`，不得依赖 application、Session 实现、TUI/CLI。
- `agent` 不感知 MCP/Skill 来源，只消费快照和标准接口。
- `features.subagents` 依赖 `application` 的窄 run/task API，不直接构造 provider adapter。

## 5. 共同核心设计

### 5.1 ToolCatalog 与一致快照

建议公开模型：

```text
ToolSourceId(kind, name)
ToolRegistration(source, tool)
ToolCatalogSnapshot(version, tools_by_name, definitions, sources)
```

规则：

1. register/unregister/reload 是原子操作；成功后版本单调递增。
2. tool name 冲突默认失败，错误同时列出两个来源；不得静默覆盖。
3. 快照持有该版本的 Tool 实例。后续撤销不使正在执行的 step 发生 name-not-found。
4. 远端连接已经失效时，MCP adapter 返回规范化失败结果，不把 transport 异常泄漏给 Agent loop。
5. `ContextPlanner` 不再长期缓存 definitions，而是在每次 plan 时接收当前 step 快照。
6. ToolRound 只能解析当前 step 快照中的 call；不能临时读取 catalog 最新版本。

### 5.2 并行 ToolRound 调度

把静态 `concurrency_safe` 属性升级为基于 invocation 的判断，例如 `is_concurrency_safe(input) -> bool`。调度算法固定为：

1. 按 AssistantMessage 中的 ToolCall 顺序扫描。
2. 连续安全调用形成一个并行组，受配置的 `max_parallel_tools` 限制。
3. 不安全调用先等待前一组结束，再单独执行，之后才开始下一组。
4. ask 类权限提示始终串行；获得授权后调用才进入并行组。
5. 任一调用失败不隐式取消同组其他调用；整个 turn 取消则取消全部未完成调用。
6. 为每个 ToolCall 生成一个且仅一个闭合 ToolResult，最终按原顺序提交。

首版只将无副作用、已确认线程/协程安全的内置工具标为安全。Write、Edit、Bash、TodoWrite 以及未知第三方工具默认不安全。MCP 工具默认不安全，除非 adapter 从可信配置得到显式声明；不能相信 server 自报字段直接放宽本地策略。

### 5.3 任务状态机与取消树

任务状态固定为：

```text
PENDING ──> RUNNING ──> SUCCEEDED
    │          ├──────> FAILED
    │          └──────> CANCELLING ──> CANCELLED
    └───────────────────────────────> CANCELLED
```

每个状态只能前进到图中的终态；终态不可变。`TaskSnapshot` 至少包含 task ID、parent task/run ID、状态、创建/开始/结束时间、结果摘要或结构化错误。取消规则：

- 取消父任务默认递归取消仍在运行的子任务。
- 超时使用同一取消路径，不创建第二套状态。
- 调用方取消等待 foreground Subagent 时，child 也必须取消。
- background ToolResult 返回 task ID 后，父 ToolCall 已闭合；以后通过 completion attachment 观察终态，并用 Read 获取 Bash 输出文件。
- `ApplicationRuntime.close()` 按“停止接收 → 取消任务 → 等待终态 → 关闭 MCP → 关闭 provider leases”的顺序执行。

### 5.4 独立 AgentRun 与 provider lease

`AgentRunFactory` 根据 `AgentRunSpec` 创建一次运行所需的完整 capsule。`AgentRunSpec` 至少声明 session、parent run/task ID、prompt/attachments、工具 allowlist、权限上限、model/provider 选择和预算上限。

不能把当前 `ProviderRouter` 直接共享给多个 run，因为它在完整 stream 期间持锁。M3 应提供独立的 `ModelClientLease`：

- 每个并发 run 有独立 client/stream 锁和关闭责任。
- provider 切换只影响之后创建的 lease；已开始的 run 使用捕获的 binding 完成或取消。
- Application 关闭会等待/取消 lease 使用者，再释放全部 SDK client。
- SDK 类型仍只存在于 `providers`。

## 6. 分阶段实施计划

里程碑按依赖而非功能列表顺序排列。M2、M3 和 M5 在 M1 后可以由不同分支并行开发；M4 必须等待 M3，M6 的 MCP Skill source 必须等待 M5。

| 里程碑 | 状态 | 直接产出 | 依赖 | 对应目标 |
| --- | --- | --- | --- | --- |
| M0 | 已完成（2026-08-23） | 基线测试、具名组装边界 | 无 | 共同基础 |
| M1 | 已完成（2026-08-23） | 动态目录与不可变 step 快照 | M0 | 五项能力的共同基础 |
| M2 | 已完成（2026-08-23） | 有屏障的 ToolRound 并行调度 | M1 | 工具并行调用 |
| M3 | 已完成（2026-08-24） | TaskSupervisor、独立 run/provider lease | M0；集成动态工具时需要 M1 | Subagent/后台任务基础 |
| M4a | 已完成（2026-08-24） | foreground child Agent | M1、M3 | Subagent |
| M4b | 已完成（2026-08-24） | 非阻塞 spawn、查询、通知和取消 | M4a | Background Task |
| M5 | 已完成（2026-08-24） | MCP 生命周期、注册和增量发现 | M1 | MCP 注册与发现 |
| M6 | 已完成（2026-08-24） | Skill discovery、lazy load 和激活 | M1；MCP source 需要 M5 | Skill 加载 |

### M0：基线固化与组装边界

状态：已完成（2026-08-23）。

依赖：无。

交付物：

- 用 dataclass 形式的 `ApplicationAssembly`/`ApplicationRuntime` 替代 `_assemble_agent()` 长 tuple，行为不变。
- 为当前串行 ToolRound、静态 registry、单 Session 和 provider switch 增加特征测试。
- 明确 tool input 在权限检查后不可被 hook 原地修改；执行阶段使用校验/冻结后的输入副本。
- 在 `tests/integration/` 建立首批真实跨组件测试，不访问真实 provider 或网络。
- 将本文的关键决策拆成 ADR（仅在实现触及对应决策时创建，避免空 ADR）。

退出条件：

- CLI 入口、现有 Session 恢复和所有测试行为无变化。
- 组装返回值不再依靠 tuple 位置解包。
- 权限判定使用的输入与 Tool 实际收到的输入一致。
- 第 10 节命令全部通过。

验收证据：

- `ApplicationAssembly` 已替代 `_assemble_agent()` 的 9 元组，组件身份由 `tests/unit/test_bootstrap.py` 验证。
- `ToolExecutor` 会隔离 submitted/approved/callback/execution input，并重新校验权限规范化后的输入；hook 原地修改与非法规范化输入由 `tests/unit/test_tools.py` 验证。
- 静态工具集的快照、稳定顺序和重名失败已在 M1 迁移为 `tests/unit/test_tool_catalog.py`；串行 ToolRound、单活动 Session 和 provider switch 继续由既有特征测试覆盖。
- `tests/integration/test_foreground_tool_loop.py` 离线贯通 Agent、Context、Session、ToolRound 和恢复路径。
- M0 完成时第 10 节五条命令全部通过，完整测试结果为 `401 passed`；架构临时豁免仍为空。

### M1：动态能力目录与 step 快照

状态：已完成（2026-08-23）。

依赖：M0。

交付物：

- 实现 `ToolCatalog` 和 `ToolCatalogSnapshot`。
- 内置工具和 Feature 工具改为带来源注册；bootstrap 不再直接拼接最终 tuple。
- `ContextPlanner` 按 step 接收 definitions；`ToolRoundExecutor` 接收同一个 tool snapshot。
- 实现原子注册/撤销、冲突诊断和版本递增。
- 静态配置下保持现有模型请求与工具行为完全等价。

退出条件：

- 测试可证明一个 step 的 request definition 版本等于 ToolRound 执行版本。
- step 中途撤销工具不影响该 step；下一 step 不再暴露该工具。
- 同名不同来源注册失败且不改变 catalog 版本/内容。
- 架构守卫没有临时豁免。

验收证据：

- `tools.catalog` 已实现 `ToolSourceId`、`ToolRegistration`、`ToolCatalog` 和不可变 `ToolCatalogSnapshot`；原静态 `tools.registry` 已移除。
- `AppState.tools` 持有唯一 application-lifetime catalog；builtin 与 Todo Feature 使用独立来源注册。
- `AgentEngine` 在 step 开始捕获一次 `ToolCatalogSnapshot`，ContextPlanner 不再持有固定 definitions，ToolRoundExecutor 显式接收同一 snapshot。
- SNAP-01 由 `tests/integration/test_capability_snapshot.py` 验证：stream 中替换 source 后，当前 step 仍执行旧实例，下一 step 才看到新 definition。
- SNAP-02 由 `tests/unit/test_tool_catalog.py` 验证：冲突注册原子失败，版本和内容不变；replace/unregister 不修改旧 snapshot。
- M1 完成时第 10 节五条命令全部通过，完整测试结果为 `403 passed`；架构临时豁免仍为空。

### M2：工具并行调用

状态：已完成（2026-08-23）。

依赖：M1。

交付物：

- 输入相关的并发安全判定和保守默认值。
- 连续安全组 + 不安全屏障调度器，以及可配置并发上限。
- 权限提示串行化、round 级取消和稳定结果排序。
- Tool lifecycle event 增加足以区分 call ID 的信息；UI 不依赖完成顺序猜测调用。

退出条件：

- 两个受控 Read 类 fake 能通过 barrier 证明同时进入执行区，不用易抖动的 sleep 计时断言。
- `safe, safe, unsafe, safe` 的观测顺序严格为“前两者可重叠；unsafe 等待二者；最后 safe 等待 unsafe”。
- 结果顺序始终与请求顺序一致，即使后一个调用先完成。
- deny/ask、单调用异常、整轮取消分别产生一一对应的闭合结果。
- 并发上限在压力测试中从未被突破。

验收证据：

- `Tool.is_concurrency_safe(input)` 已替代静态属性；Read、Glob、Grep 显式安全，其他工具保持保守默认值。
- ToolRoundExecutor 已实现连续安全组、不安全屏障、Semaphore 并发上限、组内任务回收和原顺序结果提交。
- ToolExecutor 使用独立 lock 串行化并行调用产生的 PermissionPrompt。
- settings 已支持正整数 `tools.maxParallelCalls`，默认 4；设为 1 的既有串行与取消特征测试继续通过。
- PAR-01/PAR-02/PAR-03 由 `tests/unit/test_parallel_round.py` 使用 Event/barrier 验证真实重叠、逆序完成、屏障顺序、提示串行化和并发上限；失败、拒绝与取消继续由 `tests/unit/test_tool_round_executor.py` 和 `tests/unit/test_tools.py` 覆盖。
- M2 完成时第 10 节五条命令全部通过，完整测试结果为 `408 passed`；没有新增依赖，架构临时豁免仍为空。

### M3：TaskSupervisor 与并发 run 基础

状态：已完成（2026-08-24）。

依赖：M0；接入动态工具时依赖 M1。

交付物：

- 实现 `tasks` 模块、有限状态机、父子关系、超时、取消和结果快照。
- 实现 `AgentRunFactory`、独立 run capsule 和 provider lease。
- Application runtime 显式拥有 TaskSupervisor、lease registry 和关闭顺序。
- 提供 runtime task 事件，不把 progress/delta 写入 canonical conversation。

退出条件：

- 主 run 和 child fake provider 可同时处于 stream 中，证明没有被共享 router 锁串行化。
- provider 切换不改变已经创建的 run binding，只影响新 run。
- task 成功、失败、等待时取消、运行时取消、超时和 shutdown 均只产生一个终态。
- 父任务取消会回收所有非终态子任务；测试结束没有 pending asyncio task warning。

验收证据：

- 新增零项目依赖的 `tasks` 模块：`TaskSupervisor`、`TaskHandle`、`TaskSnapshot`、单调 runtime events、父子取消树和 PENDING/RUNNING/CANCELLING/三个终态状态机。
- `application.runs` 已实现 `AgentRunSpec`、`AgentRunComponents`、`AgentRun` 和 `AgentRunFactory`；每个 capsule 拥有独立 Session、Agent 组件和关闭责任。
- `providers.leases` 已实现 `ProviderLeaseRegistry` 与 `ProviderClientLease`；每个 lease 捕获 connection、懒创建独立 SDK client 并持有自己的 stream lock。
- TASK-01 由 `tests/unit/tasks/test_supervisor.py` 覆盖成功、失败、pending/running 取消、超时、shutdown 和唯一终态；TASK-02 由 `tests/integration/test_task_cancellation.py` 验证整棵取消树无 pending task。
- RUN-01 由 `tests/integration/test_agent_run_isolation.py` 验证两个 AgentRun 的 provider stream 同时越过 barrier；`tests/unit/test_provider_leases.py` 验证 switch 前后 lease binding 隔离和 client 回收。
- SAFE-01 由 `tests/integration/test_runtime_shutdown.py` 验证 AppState 按 tasks → runs/leases → foreground provider 的顺序关闭，任务取消时 run/lease 尚未关闭，结束后无后台资源泄漏。
- M3 完成时第 10 节五条命令全部通过，完整测试结果为 `419 passed`；没有新增第三方依赖，架构临时豁免仍为空。

### M4：Subagent 与 Background Task

依赖：M1、M3。

先交付 foreground，再开放 background：

#### M4a Foreground Subagent

状态：已完成（2026-08-24）。

- 实现 `SubagentSpec`、`SubagentController` 和 spawn Tool；模型必须显式选择固定的 `explore` 或 `general` 角色。
- child 使用独立 Session、独立 run capsule、spawn 时工具快照子集和收窄后的权限。
- child 只接收显式 prompt/attachments，不复制完整父 transcript。
- child 终态被归一化为父 ToolCall 的一个 ToolResult；失败和取消同样闭合。
- 设置最大嵌套深度、每父任务最大活跃 child 数、step/token/timeout 预算。

验收证据：

- `features.subagents` 已实现 `SubagentSpec`、`SubagentController`、`SubagentTool` 和 child hierarchy/limit 值；composition root 只在 `subagents.enabled=true` 时注册 feature source。
- ToolContext 携带当前 step 的只读工具映射/version；Explore 固定取 Read/Glob/Grep/Bash 的交集并通过双重 `is_read_only()` 代理执行，General 继承完整快照。嵌套 Subagent 会绑定新的 run/task/depth identity，最大深度时移除 Subagent。
- 两个角色使用不同的专用 PromptRegistry、相同 cwd/environment 与根目录 AGENTS.md；child request 不含父 transcript。Explore 明确面向父 agent 报告文件证据，General 报告变更、验证和风险。
- child 复制父 PermissionPolicy 的 mode/rules，`AgentRunSpec.allow_permission_updates=false`，因此不能持久化或在运行期扩大父级 ask/deny；workspace 安全检查保持不变。
- child run 使用独立 UUID Session、AgentRun capsule 和 provider lease；TaskSupervisor 负责父子关系、timeout 和取消，调用方取消 foreground wait 会向 child 传播。
- 预算默认值为 depth 3、每 parent 活跃 child 4、20 steps、100000 累计 tokens 和 300 秒；均可通过 `subagents` settings 正数配置。累计 token ceiling 保留已完成响应，在下一次请求前停止继续消费。
- SUB-01 由 `tests/integration/test_subagent_lifecycle.py` 验证两份 transcript、单一父 ToolResult 和 run/lease 回收；SUB-02 由 `tests/integration/test_subagent_permissions.py` 验证能力交集、ask/deny 与 workspace 逃逸；`test_subagent_explore_safety.py` 验证 default/allow/bypass 和输入规范化都不能突破 Explore 只读边界。
- `tests/unit/test_subagent_controller.py` 覆盖深度/活跃数/step/token/timeout、失败闭合和 foreground 取消；`tests/unit/test_agent_budget.py` 覆盖累计 token ceiling。
- M4a 完成时第 10 节五条命令全部通过，完整测试结果为 `437 passed`；没有新增第三方依赖，架构临时豁免仍为空。

#### M4b Background Task

状态：已完成（2026-08-24）。

- spawn Tool 的 background 模式提交任务后立即返回 task ID，不等待 child 完成。
- 提供 `TaskList`、`TaskCancel`；Bash 输出统一通过 `Read` 获取。
- 实现完成通知的单次投递；只在 Agent step 边界注入。
- TUI/CLI 显示 task ID、状态和终态，但不直接操作 TaskSupervisor 内部结构。

验收证据：

- `Subagent(background=true)` 通过同一 SubagentController/TaskSupervisor 提交 child 后立即返回 `task_id`/`run_id`；foreground 默认行为不变。
- `TaskList`、`TaskCancel` 是标准 Tool，只能按 root owner run 查询或取消本 agent tree 的 background task；取消继续经过 PermissionPolicy。
- `BackgroundTaskNotificationSource` 只在模型规划前的 step 边界读取终态。Session 接受 durable Attachment 后由通用 acknowledgement 回写 controller；Conversation 按 task ID 去重，`inspect`、compact retry 和跨多个 step/turn 不会提前消费或重复投影。
- main/child ToolContext 都携带当前 run ID；session resume 后 main Tool 不依赖 bootstrap 时的旧 Session ID，嵌套 background task 的查询/通知归属 root agent tree。
- `backgroundTasks.enabled=false` 为默认值；TUI host 中它与 `subagents.enabled` 同时开启时，Subagent schema 才暴露 background 参数并注册 Task tools。
- BG-01/BG-02 由 `tests/integration/test_background_task.py` 使用 Event barrier 验证 submit 不等待、turn 后完成、后续 step 单次通知、run/lease 回收；不依赖耗时阈值。
- `tests/unit/test_subagent_controller.py` 验证 owner 隔离、TaskList/Output/Cancel、父任务取消树、失败/timeout/foreground cancel；M3 的 shutdown 测试继续验证 tasks → runs/leases → provider 关闭顺序。
- Task ID 与状态由 Subagent/Task Tool 的 frontend-neutral presentation 进入现有 CLI/TUI ToolStarted/ToolFinished 路径，host 不读取 TaskSupervisor 内部结构。
- M4b 完成时第 10 节五条命令全部通过，完整测试结果为 `441 passed`；没有新增第三方依赖，架构临时豁免仍为空。

退出条件：

- Foreground child 完成前父 ToolCall 保持等待；background child 启动后父 Agent 能继续下一 step。
- child 写入不会出现在 parent Session JSONL，只有显式返回结果进入父上下文。
- child 不能获得角色策略或父快照之外的工具，也不能把 ask/deny 提升为 allow。
- background 任务在“本 step 内完成、turn 结束后完成、父级取消、应用关闭”四种情形下都只通知一次或明确取消。
- 同一 parent Session 仍无法并发执行两个 foreground turn。

### M5：MCP 注册与发现

依赖：M1。可与 M3/M4 独立推进。

分两次上线，避免把连接管理、动态发现和 deferred tools 一次性耦合。

#### M5a 静态注册与生命周期

状态：已完成（2026-08-24）。

- 定义项目/用户 settings 中的 server 配置、scope、启动参数、环境变量引用和启停策略。
- 实现 `McpRuntime`、连接状态、超时、重连/关闭和结构化诊断。
- 发现远端 tools，校验 schema，适配为标准 `Tool` 并以 server source 原子注册。
- server 断开时撤销其 catalog source；已取得快照的调用返回规范化连接错误。

验收证据：

- `config.store` 解析 user/project/local 的 `mcp.servers` 完整定义；`envFrom` 只保存变量引用，literal `env` 被拒绝。共享 project 定义默认 disabled，不能开启 gate 或直接执行，local 完整副本才获得启动资格。
- `mcp.transport` 是 fake 与 stdio 共用的窄语义接口；`mcp.stdio` 零新增依赖实现 legacy 2024-11-05..2025-11-25 initialize、分页 tools/list、tools/call、超时取消和 wait/terminate/kill 关闭。
- `McpRuntime` 提供 disabled/pending/connected/failed/closed 状态、结构化清洗诊断、显式 reconnect/disconnect 和每 server 原子 source。schema、规范化名称或 catalog 冲突不会污染旧版本。
- `McpTool` 默认不声明只读/并发安全，参数在权限前由本地 schema subset 校验，并经过同一 allow/ask/deny、audit、取消和 ToolResult 归一化路径。
- `tests/unit/mcp/test_runtime.py` 使用内存 fake 覆盖 connect/discover/call/disconnect/reconnect/close、启动取消、非法 schema、名称冲突和 stale snapshot；`test_stdio.py` 使用离线 subprocess 验证 wire framing、env 最小继承和取消后继续路由。
- MCP-02 由 `tests/integration/test_mcp_tool_round.py` 覆盖 allow/ask/deny、schema 拒绝、连接失败清洗和调用取消；SAFE-01 在 M5 阶段更新为 tasks → runs → MCP → provider，并在 M6 加入两者之间的 SkillRuntime。
- M5a 完成时第 10 节五条命令全部通过，完整测试结果为 `469 passed`；`pyproject.toml`/`uv.lock` 未因 MCP 改动，没有新增第三方依赖，架构临时豁免仍为空。

#### M5b 增量发现与 provider-neutral ToolSearch

状态：已完成（2026-08-24）。

- 支持显式 refresh、server tools-changed 通知和幂等 diff。
- 完整 catalog 与每 step exposure snapshot 分离；ToolSearch 命中后从下一 step 生效。
- 为 tool name 建立稳定 namespace/显示名映射，并保留原始 server/tool identity 用于日志和权限。
- 后续再扩展 MCP resources/prompts；不得混入 M5a 的完成定义。

验收证据：

- `McpRuntime.refresh()` 对完整远端列表做 schema/name 校验和单 source replace；add/remove/update 只增加一个新 catalog version，内容相同的 refresh 不变更版本，失败保留旧 source。
- stdio 将 `notifications/tools/list_changed` 交给 runtime 的受管 refresh task。同 server 通知可合并；refresh 期间到达的新通知设置 dirty 标记并保证后续 diff，关闭时取消并等待这些 task。
- 远端 identity 保留在 `McpTool`/metadata；模型名对 `.` 做无碰撞编码，超出 64 字符时使用稳定 hash 截断，`-` 与 `_` 不再被错误合并。
- 所有 MCP adapter 始终发布到完整 catalog 并声明 searchable；全局 ToolSearch 和 Session fingerprint discovery 取代 server-local search 与数量阈值。
- 默认 dispatcher 保持顶层 definitions 稳定并经 InvokeSearchedTool 路由；native 仅从下一 step 追加有效命中。两者均不采用 Anthropic `tool_reference`/`defer_loading` 协议。
- refresh 保留 fingerprint 未变化的 discovery，定义变化或删除时失效并通知重新搜索。
- M5b 完成时第 10 节五条命令全部通过，完整测试结果为 `475 passed`；没有新增第三方依赖，架构临时豁免仍为空。

退出条件：

- 使用内存 fake transport 覆盖 connect、discover、call、disconnect、reconnect 和 close，无真实网络依赖。
- 非法 schema、重复名称、server 启动失败和调用中断不会污染已有 catalog。
- MCP 调用与内置工具一样经过参数校验、PermissionPolicy、取消和 ToolResult 归一化。
- refresh 的 add/remove/update 在下一 step 可见，当前 step 保持原快照。
- 配置诊断不泄漏 token、密钥或完整环境变量。

### M6：Skill 加载与按需激活

状态：已完成（2026-08-24）。

依赖：M1；MCP 提供的 Skill source 依赖 M5；隔离执行模式可复用 M4。

交付物：

- 定义 `SkillDefinition` schema：稳定 name、description、指令正文、source、可选 tool allowlist 和兼容性信息。
- 实现三个基础搜索层，优先级固定为 project `.my-code/skills` > user config skills > builtin；同层重名报错，不按文件遍历顺序覆盖。
- frontmatter 和正文分阶段加载：索引只载入模型选择需要的元数据，选中后才读取完整指令。
- reload 原子替换 Skill catalog；无效 Skill 隔离并生成可定位诊断，不使其他 Skill 消失。
- Skill listing 与激活正文分别使用临时/durable AttachmentMessage；需要工具化/隔离执行时复用标准 Tool/Subagent 路径。
- MCP Skill source 使用同一规范化模型和冲突规则，不建立第二套 Skill 类型。

退出条件：

- 临时目录测试覆盖优先级、同层冲突、非法 frontmatter、缺失正文、符号链接/路径越界和 reload。
- 未激活 Skill 的完整正文不会进入 ModelRequest；激活后作为 Conversation fact 持续存在且来源可追踪。
- Skill `allowed-tools` 使用通用 parser 形成 additive session allow rules；工具目录保持完整可见，deny/ask 优先。
- Skill 目录中的 Python/shell 不会因 discovery/load 自动执行。
- Skill 增删改只影响下一 step，当前 step 的 prompt/tool snapshot 不漂移。

验收证据：

- `skills.models` 定义统一的 `SkillMetadata`、`SkillIndexEntry`、`SkillDefinition`、source/fingerprint 与可定位诊断；文件系统和 `SkillRuntime.replace_source()` 提交的 MCP/其他数据来源进入同一 `SkillCatalog` 冲突规则，没有第二套类型。MCP resources transport 仍按第 11 节保持在当前范围外。
- `skills.discovery` 按 project（300）> user（200）> builtin（100）扫描 `<root>/<skill>/SKILL.md`。索引只保留 name、description、可选 `allowed-tools`/compatibility、locator 与 fingerprint；激活才读取正文。首版 frontmatter 是无新增 YAML 依赖的严格平面 `key: value` 子集，未知/嵌套/重复字段均产生隔离诊断。
- 同优先级同名候选全部排除并报告 `same_layer_conflict`，随后允许较低层确定性接管；无效 frontmatter、缺失文件/正文、超限文件、symlink 和 path escape 不会使其他 Skill 消失。
- `SkillRuntime` 先 stage 完整 discovery 结果，再原子发布 `feature:skills` Tool source 并 commit catalog。旧 `SkillTool` 持有旧 `SkillCatalogSnapshot`；reload、增删改只对下一 step 可见，索引后被修改的正文必须先 reload，不能绕过 fingerprint。
- `Skill` 是标准 Tool，继续经过 input validation、PermissionPolicy、ToolExecutor、取消与 ToolResult 闭合路径。结果保持简短；完整正文在结果 batch 后作为 durable `SkillActivationAttachment` 提交，不进入 system prompt。
- `allowed-tools` 由现有 permission-rule parser 转为 additive session allow rules。含规则的激活默认要求用户授权；失败、拒绝或取消不产生 Attachment 或 grant。多个 Skill 取规则并集，显式 deny/ask 仍优先，完整 ToolCatalog 不被隐藏。
- resume 从 durable Skill attachments 重建 session grants；compact 按 Skill 名称保留最新正文并生成 `InvokedSkillsAttachment`。
- `skills.enabled=false` 为默认值；关闭时不扫描目录、不注册 Skill Tool。启动顺序为 MCP → Skills，关闭顺序为 tasks → runs → Skills → MCP → provider，便于后续 MCP Skill source 先获得连接、后释放依赖。
- SKILL-01 由 `tests/unit/skills/test_discovery.py` 覆盖三层优先级、同层冲突、非法 frontmatter、空正文、symlink/path escape、stale index 与 Python/shell data-only；`test_skill_runtime.py` 覆盖原子 tool snapshot reload 和规范化 MCP source。
- SKILL-02 由 `tests/integration/test_skill_activation.py` 验证未激活正文不进入请求、激活正文不进入 system prompt、正文持续存在、工具目录不收窄以及 reload 只影响下一 step 的 listing/tool snapshot。失败与取消继续使用通用 ToolRound/ToolExecutor 路径。
- M6 完成时第 10 节五条命令全部通过，完整测试结果为 `487 passed`；`pyproject.toml`/`uv.lock` 未变化，没有新增第三方依赖，架构临时豁免仍为空。

## 7. 推荐的提交拆分

每个提交应保持测试可运行，不提交半完成的跨层 API。建议按以下粒度推进：

1. `refactor: name application assembly components`
2. `refactor: add immutable tool catalog snapshots`
3. `feat: schedule concurrency-safe tool groups`
4. `feat: add supervised task lifecycle`
5. `refactor: lease provider clients per agent run`
6. `feat: run foreground subagents in child sessions`
7. `feat: support background subagent tasks`
8. `feat: register tools from configured mcp servers`
9. `feat: refresh and search deferred mcp tools`
10. `feat: discover and activate skills`

不要在一个提交中同时引入新模块、迁移全部调用方并修改用户行为。先建立等价的核心边界，再逐项打开 Feature gate。

## 8. Feature gate 与回滚

建议新增独立设置，默认关闭尚未完成验收的能力：

- `tools.maxParallelCalls = 1`：设为 `1` 即回到串行执行。
- `mcp.enabled = false`：关闭 MCP source，不影响内置 catalog。
- `skills.enabled = false`：不扫描/激活 Skill。
- `subagents.enabled = false`：不注册 Subagent tools。
- `backgroundTasks.enabled = false`：只允许 foreground child。

Feature gate 只控制能力注册，不在 AgentEngine 中增加五个条件分支。关闭来源后重新生成 catalog/snapshot；正在运行的 step 按 D1 完成或取消。

## 9. 可追踪验收矩阵

实现 PR 必须在描述中列出覆盖的 Requirement ID 和对应测试。建议测试文件名是目标位置，不要求一次提前创建全部空文件。

| ID | 可观察需求 | 最低自动化验证 | 目标测试位置 |
| --- | --- | --- | --- |
| SNAP-01 | request 与 execution 使用同一 tool version | step 中途替换 catalog，旧 step 仍执行旧实例 | `tests/integration/test_capability_snapshot.py` |
| SNAP-02 | 冲突更新原子失败 | 注册同名 source 后断言版本和内容不变 | `tests/unit/test_tool_catalog.py` |
| PAR-01 | 安全调用真实重叠 | barrier/event 协调两个 fake tool 同时进入 | `tests/unit/test_parallel_round.py` |
| PAR-02 | 不安全调用形成屏障 | 记录进入/退出序列并精确断言 | `tests/unit/test_parallel_round.py` |
| PAR-03 | 结果顺序稳定且全部闭合 | 逆序完成、异常、deny、cancel 的参数化测试 | `tests/unit/test_parallel_round.py` |
| TASK-01 | 状态机只有合法转换和单一终态 | 全转换表参数化测试 | `tests/unit/tasks/test_supervisor.py` |
| TASK-02 | 取消树无泄漏 | 取消父任务后枚举 child 与 pending task | `tests/integration/test_task_cancellation.py` |
| RUN-01 | provider stream 可并发 | 两个 fake lease 同时越过 barrier | `tests/integration/test_agent_run_isolation.py` |
| SUB-01 | child Session 与父级隔离 | 比较两份 transcript/JSONL 和 result binding | `tests/integration/test_subagent_lifecycle.py` |
| SUB-02 | child 权限/工具只能收窄 | 尝试越权工具、提权模式和路径越界 | `tests/integration/test_subagent_permissions.py` |
| BG-01 | background submit 不等待完成 | submit 返回 task ID 时 child 被 barrier 阻塞 | `tests/integration/test_background_task.py` |
| BG-02 | 完成只通知一次 | 跨多个 step/turn 以 conversation task ID 去重 | `tests/integration/test_background_task.py` |
| MCP-01 | server 生命周期可回收 | fake connect/discover/reconnect/close | `tests/unit/mcp/test_runtime.py` |
| MCP-02 | MCP tool 不绕过本地安全流水线 | allow/ask/deny、取消、schema 错误 | `tests/integration/test_mcp_tool_round.py` |
| MCP-03 | discovery 更新只在下一 step 生效 | tools-changed 前后比较 snapshot version | `tests/integration/test_mcp_discovery.py` |
| SKILL-01 | 搜索优先级确定且冲突可诊断 | 三层 temp tree + 同层重复 | `tests/unit/skills/test_discovery.py` |
| SKILL-02 | lazy load 与 snapshot 稳定 | 未激活不含正文，reload 不改变当前 step | `tests/integration/test_skill_activation.py` |
| SAFE-01 | 关闭时无后台资源泄漏 | tasks → MCP → provider 的关闭序列与终态 | `tests/integration/test_runtime_shutdown.py` |
| ARCH-01 | 新依赖无环且无技术泄漏 | 扩展现有 AST guard，临时豁免保持空 | `tests/architecture/` |

涉及并发的测试优先使用 `asyncio.Event`、barrier 和受控 fake，不使用“运行时间小于 N 秒”作为核心正确性断言。真实 provider/MCP 网络测试可以作为显式 integration marker，但默认 `uv run pytest` 必须离线、确定且可重复。

## 10. 每个里程碑的质量门禁

每个里程碑退出前都运行：

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv run mycode --help
```

此外，PR 必须提供：

- 本里程碑对应的 Requirement ID → 测试名映射。
- 成功、失败、拒绝和取消路径的测试证据。
- 新增公开语义模块的静态 `__all__`。
- `tests/architecture/dependency_rules.py` 的最小新增允许边及无环证据。
- 用户可见行为或架构不变量变化所对应的文档更新。
- Feature gate 默认值与回滚方法。

不接受以下“完成”状态：只有 happy-path demo；依赖 sleep 的并发测试；后台 task 在测试结束仍 pending；通过临时架构豁免绕过依赖问题；MCP/Skill 在 ToolExecutor 外执行；或将 child 消息写进 parent Session。

## 11. 暂不包含

- 同一 Session 的多个前台 turn 并发写入。
- 后台任务跨进程恢复、分布式调度或远端 worker。
- 远端 Subagent/team 协作协议。
- MCP resources/prompts 的完整产品体验；M5 首先只承诺 tools。
- 执行任意代码的 plugin 系统。
- 依靠 OS container 提供的强隔离。
- 为所有潜在实现预建 Protocol；仍遵守“存在多个生产实现或真实扩展调用方时才抽象”的原则。

这些能力以后可以复用 `ToolCatalogSnapshot`、`TaskSupervisor` 和 `AgentRunFactory`，但不应扩大当前五项工作的验收范围。
