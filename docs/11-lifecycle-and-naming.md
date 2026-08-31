# 生命周期与命名体系

本文是项目状态、上下文和执行生命周期术语的权威定义。类型名首先表达语义角色，生命周期范围通过 `Application`、`Session`、`Run`、`Turn`、`Tool` 等前缀进一步限定。现有源码中不符合本文的名称属于待迁移项，不应作为新代码的命名先例。

## 基础词汇

| 术语 | 定义 | 应当具备 | 不应承担 |
| --- | --- | --- | --- |
| `State` | 由明确 owner 管理的权威当前数据 | 明确写入者、不变量和更新边界 | 连接、任务、锁的启停与释放职责 |
| `Snapshot` | State 或 Runtime 在某一时刻的不可变忠实副本 | point-in-time、不可变、与原 owner 解耦 | 修改原对象、隐式刷新 |
| `View` | 面向特定消费者裁剪或派生的只读投影 | 消费者安全、允许丢失内部细节 | 冒充完整权威状态 |
| `Status` | 面向展示或诊断的简短状态摘要 | 稳定字段、清楚的观测时点 | 执行控制或资源所有权 |
| `Context` | 为一次具体操作构造的只读输入、身份与能力集合 | 明确操作范围、最小依赖 | 全局 service locator、第二份权威状态、资源关闭 |
| `Environment` | 当前操作不能任意改变、但会影响执行的外部条件和能力描述 | 平台、模型能力、安全边界等明确来源 | 网络连接、client 生命周期 |
| `Cache` | 可丢弃并可从权威来源重建的非权威派生数据 | 明确失效边界 | 成为唯一事实来源 |
| `Runtime` | 拥有活资源、并发控制或 start/close/reload/switch 职责的执行设施 | owner、创建与释放顺序 | 持久化领域事实的第二份副本 |
| `Execution` | 工具、命令等能力的一次具体执行 | 开始、终态、失败和取消语义 | 表示长期资源容器 |

`Runtime` 不等于“进程全局对象”。它的生命周期只需长于一次具体操作，因此可以存在 application-scoped、session-scoped 或 run-scoped runtime。名称应通过前缀说明范围，例如 `ApplicationRuntime`、`SessionRuntime` 和 `McpRuntime`。

`State` 的判断重点是权威性，而不只是 Python 对象是否可变。内部使用可变容器但内容完全可重建的对象通常是 Cache；拥有 socket、task、lease 或 close 顺序的对象通常是 Runtime。

类型名优先使用“范围或领域 + 语义角色”，例如 `SessionContextCache`、`ToolExecutionContext`、`ApplicationStatus`。不要只使用 `State`、`Context`、`Runtime` 这种脱离范围后无法判断职责的名称，也不要为了统一后缀把不同语义压进同一类别。

## 对话与执行层级

```text
ApplicationRuntime
├── Active Session / SessionRuntime
│   ├── Run
│   │   ├── Invocation
│   │   │   ├── Turn
│   │   │   ├── Step
│   │   │   │   ├── ModelCall
│   │   │   │   └── ToolRound
│   │   │   │       └── ToolExecution
│   │   │   └── Turn ...
│   │   └── Invocation ...
│   └── Run ...
└── child Run ...
```

图表示概念包含关系，不要求每个层级都对应一个聚合对象。一场 interactive Invocation 可以在安全 step boundary 接受新的用户输入，因此一次 Invocation 可以覆盖多个 Turn。

### Application

一个 `mycode` 应用实例的进程级生命周期边界。它拥有 workspace、Provider、Tool catalog、任务监督、扩展设施、会话目录和关闭顺序。它不是模型输入意义上的 Context，因此进程级根对象优先命名为 `ApplicationRuntime`，不命名为 `ApplicationContext`。

### Session

可持久化、可恢复的对话事实边界。Session 保存跨多个 Turn 的 canonical Conversation、compact、request audit 和恢复所需事实。Session 可以在不同进程中先后激活；是否当前驻留内存不改变其身份。

磁盘中的 Session catalog、当前唯一的 foreground Session，以及并发 child run 使用的 Session 是三种不同的所有权关系，不应以“进程中有多个 Session”推导出所有历史 Session 都必须加载。

### Run

一个 Agent 或 Session 在当前进程中的一次 live activation。Run 可以拥有 Agent 组件、Session 绑定、Context cache、Provider lease 和 close 行为。Session 是持久化身份，Run 是执行身份；二者不得因为当前实现复用同一个 UUID 而视为同一概念。

### Invocation

一次上层 Agent 执行，从 `submit`、`stream` 或 continuation 启动，直到 succeeded、failed、cancelled 或 limit 终态。Invocation 可以包含多个 Step，也可以因 step-boundary steering 接受多个 Turn。

持久化 journal 或可观测性 span 如果覆盖整个 Agent loop，应使用 Invocation 语义，不应仅因最初由用户输入触发就命名为 Turn。

### Turn

Conversation 中由一条已接受用户输入发起、到下一条已接受用户输入之前的因果片段。一个 Turn 可以包含多个模型 Step 和 ToolRound。相邻批量接受的多条 HumanMessage 各自建立用户输入边界；展示层可以聚合，但不改变 canonical 边界。

### Step

Invocation 内的一次模型调用及其可选 ToolRound。模型完整响应提交为 `AssistantMessage`；若包含 ToolCall，则对应 ToolRound 完整闭合后 Step 才到达安全边界。

### Task

可以异步观察、等待或取消的进程内工作单元。Task 描述调度和终态，不等于 Session、Run 或 Conversation fact。后台 Bash 和 Subagent 可以都由同一个 Task supervisor 管理，但保留各自的产品所有权。

## 其他领域术语

- `ToolRound`：同一 `AssistantMessage` 中 ToolCall 的分组执行，以及按原调用顺序形成完整 `ToolResultBatch` 的闭合边界。
- `Conversation facts`：Session 私有持有并按语义方法提交的 provider-neutral canonical entries。
- `Compact`：提交 summary、content replacement 和 compact boundary，建立新的 context 工作集，不删除完整 transcript。
- `Tool source`：以内置能力、Feature、MCP 或 Skill 等稳定来源 ID 原子发布到 `ToolCatalog` 的一组标准 Tool。
- `Model context`：从 Session snapshot、prompt、工具定义和 Environment 投影出的一次模型输入；不是 Session 的别名，也不是第二份可写 history。

## Request、Call、Attempt 与 Response

这些术语分别描述数据和行为，不应互换：

```text
ModelRequest                    要发送的数据值
  -> ModelCall                  发送并等待结果的行为
       -> Attempt 1             第一次实际尝试
       -> Attempt 2             重试时的第二次尝试
  -> ModelResponse              成功返回的数据值
```

- `Request` 和 `Response` 是 provider-neutral 或 wire-level 数据。
- `Call` 是跨越 Provider、API 或其他外部边界的行为及其生命周期。
- `Attempt` 是 retry 策略产生的一次实际尝试。
- `Invocation` 是可能协调多个 Call、ToolRound 和状态提交的上层执行。
- `Execution` 优先用于 Tool、Command 等本地或受监督能力的实际执行。

命名中应保留层级，例如 `ModelRequest`、`ModelCall`、`ModelCallAttempt`、`ToolExecution`，避免没有领域前缀的 `RequestContext` 或 `ExecutionState`。

## Context 使用规则

Context 必须绑定具体操作，例如：

- `ToolExecutionContext`：一次工具执行需要的 workspace、run identity 和工具快照；
- `RunObservationContext`：一次 Run/Invocation 的观测标签；
- `EvaluationContext`：一次评测操作的关联身份；
- `ContextPlanningInput`：模型上下文规划所需的只读输入。

以下用法应避免：

- `ApplicationContext` 持有全部服务和可变状态；
- Context 暴露任意服务查找方法；
- Context 自己启动或关闭其引用的资源；
- 把跨 Session 的可变 cache 放进一次操作的 Context；
- 用 Context 同时表示模型上下文窗口、依赖注入容器和 UI 状态。

当“context”专指模型输入时，使用 `ModelContext`、`ContextPlan`、`ContextBudget` 等明确名称；当它表示执行参数时，必须带操作前缀。

## Snapshot、View 与 Status 的选择

- 需要完整保留某个 owner 在时点上的可观察数据，使用 `Snapshot`。
- 面向 TUI、API 或特定 use case 裁剪字段，使用 `View`。
- 只表达健康度、计数、当前模式等简短摘要，使用 `Status`。

`ApplicationStatus` 不是 Runtime；它只是 Runtime 的状态 DTO。此类名称应使用 `ApplicationStatus`、`ProviderStatus`，或在确实需要强调来源时使用 `ApplicationStatusView`。

## 所有权与命名检查

新增长生命周期类型前必须回答：

1. 谁创建和拥有它？
2. 它在哪个事件后可见？
3. 谁可以更新它？
4. 它何时失效或被替换？
5. 谁负责关闭或释放？
6. 它是否持久化，权威来源在哪里？
7. 它属于 State、Cache、Runtime、Snapshot、View 还是 Context？

如果一个类型同时符合多个类别，应先拆分所有权，而不是选择一个更宽泛的名字掩盖混合职责。现有类型的迁移计划见 [refactor/todolist.md](refactor/todolist.md)。
