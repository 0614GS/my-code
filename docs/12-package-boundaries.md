# 包边界与依赖方向

> 状态：Accepted，迁移已完成。第 1 至第 7 步均已落地并通过验收。

Provider availability、model capabilities、actual usage、local estimate 与 compact policy
是五个独立维度：Provider adapter 只处理 wire payload，`providers` 的目录端口与缓存解析
模型能力，`context` 对完整请求计量并实施策略，application DTO 才向 TUI 暴露结果。TUI
不得导入 SDK、缓存文件或 Provider 原始 payload。

## 1. 背景

迁移启动时，nano-code 同时采用了两种组织方式：Agent、Context、Tools、Sessions 等按技术子系统
分包，Todo 和 `@path` 等能力则开始纵向穿过多个子系统。随着能力增加，一些概念的所有权
已经不再清晰：

- 顶层 `attachments.py` 同时处理输入语法、TUI 编辑、工作区索引、权限和工具执行；
- attachment 的模型、派生、投影与会话交付分别散落在 `messages`、`context` 和
  `agent.contracts`；
- Todo 既是 `TodoWrite` 工具的输入，也是可恢复的会话投影、模型 reminder 和 TUI 状态；
- 前端无关的 `ChatRuntime` 契约位于 `tui`，其实现却位于 `cli`；
- 一些相同语义的值对象在实现层和前端契约中重复定义。

这些问题目前没有破坏运行时行为，但会让未来的 Hooks、MCP、Skills、Plan、Web 前端和
更多 session-scoped projection 难以判断接入位置。本文先约束依赖方向和类型所有权，再据此
规划迁移；不以“减少顶层目录”或“所有文件放到一个领域包”为目标。

## 2. 参考源码的使用边界

Claude Code 仍作为行为与生命周期参考，但不作为目录模板。

值得保留的语义包括：

- 用户输入、模型请求、Transcript 和 UI 事件是不同边界；
- attachment 是不必写入 Transcript 的模型可见上下文，不等同于普通会话消息；
- TodoWrite 是更新会话任务状态的工具入口，任务状态还会被恢复、提醒和展示；
- 文件候选、`@path` 提取和文件内容注入是不同阶段。

参考源码自身也存在不适合复制的组织方式。例如
`claude-code/src/utils/attachments.ts` 同时聚合文件、Todo、Task、Memory、Skill、Hook 等
大量 attachment 类型和生成逻辑；TodoWrite 位于 `tools/TodoWriteTool/`，Todo 类型和恢复
逻辑却又分布在 `utils/todo/`、AppState 和 session restore 中。nano-code 应学习这些机制的
不变量，而不是复刻其历史形成的文件布局。

## 3. 目标架构

### 3.1 四类包

项目中的包按责任分为四类：

| 类别 | 责任 | 示例 |
| --- | --- | --- |
| 核心机制 | 表达 Agent、Conversation、Context、Tool 等稳定运行机制 | `agent`、`conversation`、`context`、`tools` |
| 产品能力 | 纵向拥有一个用户或模型可感知的能力及其不变量 | `features.todos`、`features.file_mentions`、`features.subagents`、`features.plan_mode` |
| 驱动/被驱动适配器 | 将外部协议、存储或 UI 映射到应用与核心端口 | `tui`、`cli`、`providers`、`sessions` |
| 组合根 | 创建具体对象并连接端口，不拥有业务规则 | `core.bootstrap` |

顶层包数量不是判断边界好坏的标准。一个能力如果拥有独立术语、不变量和生命周期，并且
被多个机制消费，可以成为 `features/` 下的独立能力包；只为某个入口提供格式转换的代码
不应伪装成产品能力。

### 3.2 依赖方向

目标依赖方向如下：

```text
CLI / TUI adapters ───> application.chat contracts <── application.chat runtime
                                                             │
                                                             ▼
                                                      Agent inbound port
                                                             │
                                                             ├──> Context port <── Context implementation
                                                             ├──> Model port   <── Provider adapter
                                                             ├──> Tool port    <── Tool execution adapter
                                                             └──> Session port <── Session adapter

Product capabilities expose typed APIs to the layer orchestrating them;
they do not depend on CLI/TUI adapters.

core.bootstrap 只负责从外向内组装上述对象。
```

必须遵守以下约束：

1. TUI 不拥有其他前端也要实现或消费的应用契约。
2. CLI 不拥有交互式应用运行时；CLI 只是该运行时的一个入口。
3. Agent 核心不导入 Textual、provider SDK、JSONL record 或具体存储实现。
4. Context 不读取 TUI 输入，也不执行具体工具；它只组合、投影和计量模型上下文。
5. 适配器之间不通过彼此的内部类型通信，而是共同依赖应用或核心端口。
6. 所有工具调用来源都经过同一个受控执行边界，不允许某项能力直接调用
   `Tool.execute()` 形成旁路。

## 4. 类型所有权规则

项目不新增全局 `types.py` 或顶层 `types/` 杂物包。类型按照其语义和不变量的所有者放置，
而不是按照“被多少模块 import”放置。

### 4.1 领域值对象

负责校验业务不变量的类型与所属能力放在一起。例如 `TodoItem` 属于 Todo 能力；它不因为
TUI 也要展示就变成 TUI DTO。

### 4.2 Port contract

跨边界的输入、输出、事件和 Protocol 由声明该端口的一侧拥有：

- Agent 对外能力放在 `agent.contracts` / `agent.ports`；
- 前端所需的稳定对话 contract 和用例实现放在 `application.chat`；
- provider wire type 只存在于 provider adapter 内。

Port contract 应只携带调用方确实需要的数据，不泄漏具体适配器或内部可变对象。
`application.chat` 不包含 Textual 组件；它拥有 Chat port、前端无关 DTO、展示语义和用例
实现。Tool/Agent/feature 可以依赖其中稳定的 contract 子模块，但不能依赖 runtime 实现。

### 4.3 Adapter DTO

只有当两侧语义、可信度或生命周期确实不同，才复制结构相似的 DTO，并在边界处显式映射。
例如 Agent turn outcome 与安全的前端 outcome 可以是两个类型；内部 `PathSuggestion` 与
TUI `PathSuggestion` 若语义完全一致，则应合并到应用 contract，而不是机械复制。

### 4.4 持久化 record

JSONL record 由 `sessions` adapter 拥有。Conversation model 不携带 schema version、磁盘
字段名或兼容解析逻辑；codec 是 record 与 conversation model 的唯一映射边界。

### 4.5 Presentation

前端无关的展示 contract 由 `application.chat` 拥有，具体颜色、布局和组件由前端拥有。
当前顶层 `presentation.py` 中的 `ToolUsePresentation`、`ToolResultPresentation` 应迁入
`application.chat.presentation`；Tool 或其他能力实现只负责生成这些稳定值对象，不依赖
具体前端或 runtime 实现。

## 5. Attachment 的边界

### 5.1 术语

后续代码和文档统一区分：

| 术语 | 含义 | 所有者 |
| --- | --- | --- |
| `ContextAttachment` | 不写 Transcript 的模型可见附加内容 | Context contract |
| attachment source | 从会话快照派生 attachment | Context |
| attachment projection | 将 attachment 转为 provider-neutral model message | Context |
| attachment delivery | 将 live-session attachment 锚定到 working set | Agent/Conversation contract |
| file mention | 用户输入中的 `@path` 语法和值对象 | File mention capability |
| path suggestion | 输入补全使用的工作区候选 | File mention capability / Application Chat contract |

“attachment”描述上下文交付机制，不表示文件、Todo、Hook 等能力都属于同一个 attachment
领域。各能力只在边界处产出 `ContextAttachment`。

### 5.2 建议布局

```text
context/
  attachments/
    models.py          # ContextAttachment、retention、content block
    sources.py         # DerivedAttachmentSource / Resolver
    projection.py      # ContextAttachment → ModelUserMessage

features/
  file_mentions/
    models.py          # FileMention、line range
    parser.py          # 提交文本中的 mention 解析
    loader.py          # mention → attachment 的应用用例
    reader.py          # 独立、有界的 workspace 读取
    suggestions.py     # 工作区路径候选端口/实现

tui/
  completion.py        # 光标位置识别、候选选择和文本替换
```

`mention_at_cursor()` 和候选插入属于编辑器行为，不能与提交后解析共享一个含混的顶层模块。
两者可以复用 `FileMention` 等值对象，但分别保持交互语义和提交语义。

### 5.3 工具执行与授权

File mention loader 使用独立的 workspace reader，不取得 ToolRegistry、ToolExecutor、ToolCall、
hook、audit 或工具结果存储。中立 workspace 安全层由文件工具和 mention reader 共用，集中
维护 canonical path、工作区/符号链接边界、展示路径和 path-rule 匹配；两条路径各自实现 I/O。

显式 mention 本身满足普通 ask，但不能覆盖 Read/Glob 的 whole-tool deny、path deny、工作区
逃逸或符号链接逃逸。reader 对文件执行 UTF-8、NUL、8 MiB、范围和默认 2000 行限制；目录
只稳定列出最多 500 个直接子项并跳过越界符号链接。单项失败继续处理后续 mention，取消
继续传播。PermissionPolicy 是动态对象，新增或替换规则会在下一次读取即时生效。

## 6. Todo 的边界

### 6.1 定位

TodoWrite 是工具，但 Todo 不只是工具。当前 Todo 同时拥有：

- 输入和状态不变量；
- 从已提交 Transcript 事实得到当前列表的投影；
- session resume 语义；
- 模型 reminder；
- Agent 状态和实时更新；
- 前端展示。

因此本文将 Todo 定位为 `features/` 下的一等产品能力，工具只是写入该能力的一个入口。
不应仅因 TodoPanel 存在就把 Todo 模型放进 TUI，也不应把 reminder 和会话投影塞进单个
`todo_write.py`。

建议的能力内部结构为：

```text
features/
  todos/
    models.py          # TodoItem、TodoStatus
    codec.py           # TodoWrite JSON input ↔ Todo model
    projection.py      # Conversation history → current TodoList
    reminder.py        # TodoList → ContextAttachment source

tools/builtin/todo_write.py  # Tool adapter
```

### 6.2 Agent 的具体 Todo 依赖

当前 Agent status 和 event 显式携带 `TodoItem`，AgentEngine 也会在工具轮提交后比较 Todo
投影。这是一个明确的产品耦合，但在只有一种 session projection 时仍比无类型的
`dict[str, object]` extension 更安全。

Plan Mode 的状态不持久化，计划正文只是普通 AssistantMessage，因此它与 Todo 不共享
session projection 生命周期，不构成提取通用 projection registry 的理由。Todo 继续保留
显式类型，并在依赖图中标记为有意的产品依赖。未来只有出现第二个确实需要从 Conversation
事实重建的结构化状态时，才重新评估 `ConversationProjection[T]`。

## 7. Application 与前端边界

迁移前 `tui.contracts.ChatRuntime` 是前端无关的应用端口，`cli.runtime.CliChatRuntime` 则是
该端口的主要实现。它们的目录名与真实职责相反。

建议迁移为：

```text
application/
  chat/
    contracts.py       # ChatRuntime、RuntimeStatus、TurnEvent、HistoryEntry
    presentation.py    # ToolUsePresentation、ToolResultPresentation
    runtime.py         # ChatRuntime 默认实现
    permissions.py     # DeferredPermissionPrompter
    projections.py     # Agent contract → Chat DTO

cli/
  arguments.py
  main.py              # print/TUI 启动入口
  services.py          # 仅 CLI 特有 adapter；可继续拆分

tui/
  app.py
  widgets.py
  screens/
```

TUI、测试前端和未来 Web adapter 都依赖 `application.chat` contract。Application runtime
实现该 contract，不能反向 import TUI。Provider profile、session catalog 等 DTO 若已是稳定
Chat contract，可以暂时复用；若开始泄漏存储或 SDK 细节，再在 Application Chat 边界建立
专用 DTO。

## 8. 目标目录草图

下面是方向性草图，不要求一次迁移完成，也不表示所有现有包都必须改名：

```text
src/nano_code/
  agent/
    contracts/
    ports/
    engine.py

  conversation/          # 会话事实及其紧邻的 content/primitives
    models.py
    state.py

  context/
    attachments/
    compaction/
    planner.py

  tools/
    builtin/
    contracts.py
    executor.py
    registry.py

  features/
    file_mentions/
    todos/
    subagents/
    plan_mode/

  application/
    chat/

  tui/
  cli/
  providers/
  sessions/
  permissions/
  prompts/
  core/
    bootstrap.py
```

已确认将原 `messages` 改名为 `conversation`。迁移不能只改目录名：需要同时确认
`TextContent`、`JsonObject`、`TokenUsage` 等 primitive 的最终所有者，避免换名后继续保留
相同的含混依赖。Conversation 包只拥有会话事实及紧邻的内容值对象，不成为新的共享类型
杂物包。

当前 `TextContent`、`ToolCall` 和 `ToolResult` 归 `conversation`；`TokenUsage`、
`JsonObject`/`JsonValue` 归 provider-neutral 的 `model`。Conversation 只引用这些值，
不再通过 Agent 或 Provider 转发共享类型。

`ContextInstruction`、`UserContextDocument` 和 XML marker 投影归 `context`；
`ContextAttachment` 及 retention/content block 归 `context.attachments.models`。Context 和
attachment 包入口保持轻量，source、projection、planner 等实现必须从明确子模块导入，避免
Agent contract 只引用 attachment model 时初始化整个 Context adapter。

阶段 3 已建立 `conversation.state.Conversation` 纯内存聚合；具体 `sessions.Session`
持有它并执行持久化优先提交。旧 `ConversationState`、`SessionRepository` 和 Agent-owned
session contract 已删除。live-session attachment delivery 由 `context.ContextSession` 持有，
工具展示由 `tools` 定义并作为 Session add-on record/index 恢复。

## 9. 后续能力对边界的要求

### 9.1 Explorer Subagent

Subagent 首版只服务代码库探索，通过 Agent Tool 被父模型调用。完整能力不能放进单个 Tool
类，建议结构为：

```text
features/
  subagents/
    models.py          # explorer request、result、status
    service.py         # 创建、运行、取消和 usage/result 汇总
    context.py         # 父子上下文与只读能力继承策略

tools/builtin/agent.py # Agent Tool adapter
```

Agent Tool 只负责 schema、权限、展示和把调用委托给 Subagent service。该 service 通过明确的
`AgentSpawnerPort`/factory 创建子 Agent，不能 import `core.bootstrap`，也不能复用父 Agent
的可变 `ConversationState`。Explorer 拥有独立运行状态、取消范围和上下文预算；首版只暴露
探索所需的只读工具，不承担通用任务执行、后台任务、团队协作或嵌套 Subagent。

Explorer 的最终文本作为正常 tool result 回到父 Agent。它不把中间 AssistantMessage 混入
父 Conversation；若需要展示进度，由 `application.chat` 定义前端无关事件，TUI 只负责
展示。父工具调用被取消时必须向下取消 Explorer，并为父 Conversation 生成协议完整的
tool result。

### 9.2 Plan Mode

Plan Mode 是 `features.plan_mode`，但不是需要持久化的会话状态机。首版语义是一次临时的
“先探索并输出计划”交互模式：

```text
用户进入 Plan Mode
  → 注入 plan-specific prompt 和只读工具约束
  → Agent 使用 Read/Search/Explorer Subagent 调研
  → 最终输出一条普通 AssistantMessage 作为计划正文
  → Application Chat 展示是否执行的选择
  ├── 执行：退出 Plan Mode，以普通模式继续实现
  └── 不执行/反馈：保留计划消息，由用户补充意见或结束
```

计划正文不使用 attachment、专用 plan file 或新的 Transcript record；它和其他模型回复一样
持久化为 AssistantMessage。Plan Mode 的 runtime flag 不持久化，resume 后默认回到普通模式；
历史计划仍作为普通对话内容可见，用户可以再次要求执行或修改。

Plan Mode 至少包含：

- mode-specific prompt/context source；
- 计划期间的只读 Tool availability policy；
- 最终计划识别与 application-level approval request；
- 批准、拒绝和反馈的 typed Chat contract。

Plan Mode 对写工具的限制应由 availability policy 在工具暴露与执行前强制实施；Permission
仍负责对“当前模式允许尝试的调用”做授权。这样即使存在 allow rule 或
`bypassPermissions`，也不能意外绕过 Plan Mode 的只读约束。

首版不需要让模型调用 Enter/Exit Plan Mode Tool。模式由 Application Chat 接收用户操作后
设置；TUI 不直接修改 Agent 或 PermissionPolicy。用户批准执行后，Application 退出 Plan
Mode，并追加一条表达“执行上面的计划”的真实 HumanMessage，作为后续实现回合的明确输入。
这条消息正常写入 Transcript，因此崩溃或 resume 后仍能区分“只生成了计划”和“用户已批准
执行”。实现阶段模型可以使用 TodoWrite 向用户展示执行进度，Todo 状态仍遵循现有
Conversation projection 语义。

### 9.3 Explorer 与 Plan Mode 的组合约束

Plan Mode 可以调用 Explorer Subagent，但不能通过子 Agent 绕过只读限制。父 Agent 创建
Explorer 时传入显式、缩减后的执行 scope：

```text
ExplorerExecutionScope
  ├── parent/child identity
  ├── cwd 与允许探索的工作区边界
  ├── read-only tool capability set
  ├── permission rules snapshot
  ├── provider/model 与上下文预算
  └── cancellation scope
```

该 scope 是创建 Explorer run 时的强类型输入，不是进程级全局变量。Explorer 不继承父
Agent 的 live-session attachment delivery，也不能写入父 Conversation；父 Agent 只接收最终
tool result。所有 inherited allow 都必须先与 Explorer 的只读 capability 取交集，deny 始终
保留。

用户批准 Agent Tool 时，同时授权该次 Explorer 在既定工作区和只读 capability 内执行探索，
子 Agent 不再向用户发起权限询问。普通 read `ask` 在该次委托中视为已获授权；whole-tool
deny、path deny、工作区/符号链接逃逸和工具 safety deny 不可被覆盖。若某次调用不属于明确
允许的只读能力，或仍需要无法自动满足的安全确认，则 fail closed，并把错误返回 Explorer/
父 Agent，而不是弹出权限面板。

## 10. 迁移原则与顺序

迁移必须保持行为不变，并分成可独立审查的小步：

1. **建立边界测试**：记录允许的包依赖方向，为关键包增加 import smoke tests 或静态依赖
   检查；保持现有行为测试通过。
2. **纠正 Application Chat 所有权**：建立明确的 `application/chat` 包，将
   `tui.contracts` 的稳定 DTO/Protocol 和顶层 `presentation.py` 移入其中，将
   `cli.runtime` 的用例实现移为 `application.chat.runtime`，保留临时 re-export，避免同时
   修改所有调用方。
3. **建立 features 并拆分 file mention**：把提交解析、TUI 光标行为、候选索引和加载用例
   分开；删除含混的
   顶层 `attachments.py`。
4. **重命名 conversation 并收拢 attachment**：将 `messages` 迁为 `conversation`，移动
   attachment model、source 和 projection；保持
   `AttachmentDelivery` 的 Agent/Conversation 锚点语义不变。
5. **关闭工具执行旁路**：让显式用户读取经过统一 invocation 边界，并覆盖 deny、取消、
   hook/审计失败和异常路径测试。
6. **迁移 Todo feature**：保留强类型产品能力，明确 Tool adapter 与会话投影的关系；Plan
   Mode 不共享该投影机制。
7. **最后处理命名与公开导出**：删除过渡 re-export，更新文档和 import，检查循环依赖。

### 10.1 当前迁移进度

- [x] 第 1 步：新增静态 import 边界测试，约束 Application Chat 不反向依赖 CLI/TUI，核心
  机制不依赖 Chat runtime，并禁止生产代码重新使用旧权威路径。
- [x] 第 2 步：`application/chat` 已成为 Chat contract、presentation、permission bridge 和
  默认 runtime 的权威位置；生产调用方已切换到新路径。
- [x] 第 3 步：`features.file_mentions` 已分别拥有模型、提交解析、加载用例和候选索引；
  光标识别及候选文本插入由 `tui.completion` 拥有，顶层 `attachments.py` 已删除。
- [x] 第 4 步：会话事实与 primitives 已迁入 `conversation`；用户上下文值对象、XML 投影和
  attachment model/source/projection 已归入 `context` 的明确子模块，原 `messages` 包已删除。
- [x] 第 5 步：file mention 使用带来源、授权证据和 inline 结果模式的统一 Tool invocation；
  deny、显式 ask、取消、hook/审计失败和部分失败均有回归测试。
- [x] 第 6 步：Todo 模型、Tool input codec、Conversation 投影和 reminder 已迁入
  `features.todos`；`TodoWriteTool` 保持为只依赖 codec 的薄适配器。
- [x] 第 7 步：删除 `nano_code.tui.contracts`、`nano_code.cli.runtime` 和
  `nano_code.presentation` 的临时 re-export，并完成公开导出清理。

第 2 步曾保留的三个兼容模块现已删除；`CliChatRuntime` 过渡别名也不再公开。TUI 包根只
公开具体 UI 适配器，不再转发 Application Chat contract。`application.chat.__init__` 刻意
保持轻量；调用方从明确的 `contracts`、`presentation`、`permissions` 或 `runtime` 子模块
导入，避免只需要底层展示值对象时初始化完整的 Application runtime。

每一步都必须运行 `ruff format --check .`、`ruff check .`、`pyright` 和 `pytest`。涉及权限
或执行管线的步骤必须额外覆盖 deny 不可绕过、ask 的显式授权语义、取消和部分失败。

## 11. 迁移期间的不变量

无论最终目录如何选择，迁移不得改变以下行为：

1. Transcript 只保存原始用户提示和真实模型/工具会话事实，不保存临时 attachment 正文。
2. `live_session` attachment 在当前进程后续请求中按锚点可见，resume 后清空。
3. attachment projection 只能产生 `ModelUserMessage`，不得产生 assistant/tool 协议块。
4. `@path` 的显式授权不覆盖任何 deny 或工作区安全边界。
5. Todo 状态只来自已成功提交的 TodoWrite 会话事实；失败调用不更新 UI。
6. TUI 不读取 Transcript、Provider SDK、ToolExecutor 或 ContextPlanner 内部状态。
7. UI 只消费 reasoning presentation；continuation payload、签名、redacted data 和密文不得越过 Application 边界。
8. Provider 原始索引和序号只用于 adapter 内关联；Model envelope 的规范化序号在 Agent
   校验后剥离，Application/TUI 仅消费无 ID 的展示生命周期。
9. 组合根继续注入同一套运行时 Tool、Permission 和 workspace 能力，不复制动态配置。
10. Plan 正文只是一条普通 AssistantMessage；不新增 plan record、attachment 或文件事实来源。
11. Plan runtime flag 不持久化，resume 后回到普通模式。
11. Explorer Subagent 不能写入父 Conversation，也不能扩大父级权限或 Plan Mode 能力范围。
12. Explorer 不向用户请求权限；受委托的只读 ask 自动允许，其他不能自动允许的调用
    fail closed。

## 12. 已确认决策与延后事项

### 12.1 已确认

1. 产品能力统一放入 `features/`。
2. File mention 复用 Read/Glob Tool，并进入统一 Tool invocation 管线，不抽取平行的
   `WorkspaceReader` 执行路径。
3. `messages` 改名为 `conversation`。
4. 前端无关的 presentation contract、Chat port 和 runtime 归入 `application/chat/`。
5. 建立明确的 `application/` 包。
6. Plan 正文是普通 AssistantMessage；Plan Mode 状态不持久化，resume 后使用普通模式。
7. Plan Mode 只负责探索后输出计划并让用户选择是否执行；批准后进入普通实现流程，TodoWrite
   可用于展示执行进度。
8. Subagent 首版收窄为 Explorer，不设计通用执行 Agent、后台任务、团队协作或嵌套调用。
9. 用户批准执行计划时，追加一条真实 HumanMessage 并正常写入 Transcript。
10. 用户批准 Agent Tool 时同时授权该次 Explorer 的受限工作区读取；Explorer 不向用户询问
    权限，deny、越界和 safety deny 仍然 fail closed。

### 12.2 延后事项

当前只保留一个延后决策，不阻塞本轮迁移和 Explorer 首版接口设计：

1. **Explorer trace 是否持久化**：Explorer 的内部 Conversation 完全临时，还是写入只用于
   诊断的 sidechain transcript，后续实现前再确定。无论选择哪种，父 Conversation 都只保存
   Agent Tool call/result，且不会恢复未完成 Explorer 继续运行。

该事项不改变已经确认的包方向：feature 拥有能力不变量，Tool 只是模型调用入口，
`application.chat` 拥有前端 contract 和用例实现，具体 adapter 不互相拥有。
