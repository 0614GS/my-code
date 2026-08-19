# 状态所有权与生命周期

本文件回答两个问题：某份数据由谁维护，以及它在何时创建、恢复和销毁。类型所有权与存储实现必须分开；`sessions` 可以序列化消息，但不能因此拥有消息语义。

## 消息与上下文边界

| 数据 | 语义所有者 | 运行时维护者 | 是否持久化 |
| --- | --- | --- | --- |
| `HumanMessage`、`AssistantMessage`、`ToolCall`、`ToolResult`、summary | `conversation` | 当前 `Session` 内的 `Conversation` | 由 `sessions` 编码到 JSONL |
| 活动父链、完整 history、compact 后 working set | `conversation` | `Conversation` | 由 message、replacement、boundary 重建 |
| JSONL record、schema version、codec、catalog | `sessions` | `SessionStore` | 是 |
| session ID、metadata、提交顺序、恢复事务 | `sessions` | `Session` | 是 |
| 工具历史展示快照 | `tools` 定义，`sessions` 编码和索引 | `Session` | 是，但不是模型对话事实 |
| request attachment | `context` | 单次 context build | 否 |
| live-session attachment delivery | `context` | `ContextSession` | 否；resume 或切换 session 后清空 |
| `ModelMessage`、`ModelRequest`、budget | `model` 定义，`context` 构造 | 单次模型请求 | 否 |
| provider wire payload 与流解析状态 | `providers` | 单次 provider call | 否 |

数据流只有一个方向：

```text
JSONL --sessions 恢复--> Conversation
ConversationSnapshot + ContextSession --context 投影--> ModelRequest
ModelRequest --providers 映射--> provider wire payload

模型输出 --agent 转为对话事实--> Session.commit(...)
Session --先写 JSONL，后更新内存--> Conversation
```

`context` 不保存第二份 conversation messages，`model` 不保存历史，Provider 不接触 JSONL。`ModelRequest` 是一次调用的不可变快照；下一次调用必须从最新 `ConversationSnapshot` 重新构建。

## Session 与 Conversation

`Conversation` 是纯内存聚合，拥有消息顺序、父链、working set、replacement 和 compact boundary 的不变量。它不进行文件 I/O。

`Session` 为一个 `Conversation` 增加 session ID、持久化提交和恢复生命周期。运行期间只能通过 `Session` 提交可恢复事实。阶段 3 已实现以下顺序：

1. 在内存副本上验证变更；
2. 将 record 追加到 JSONL；
3. 写入成功后替换或更新内存中的 `Conversation`；
4. 写入失败时保持原内存状态。

活动 session 只在创建或显式 resume 时完整 hydration 一次，之后不把磁盘当作 refresh API。进程存活时，`Conversation` 是当前活动状态；JSONL 是跨进程恢复依据。

恢复由 `sessions` 完成解析、schema 校验、父链重建、compact working set 重建和未闭合工具轮修复。`chat` 只有在目标 session 完整恢复成功后，才原子替换当前活动状态；失败时继续使用原 session。

一次切换需要同时替换：

- `Session` 及其 `Conversation`；
- `ContextSession`；
- session 对应的工具结果目录和展示索引。

当前 provider/profile、runtime permission policy 和工作区不从 Transcript 恢复。`session_started` 中的对应字段只用于历史诊断；继续对话使用当前运行时配置，provider continuation 仅在 binding 匹配时回放。

## Context 状态

`context` 同时包含无状态投影逻辑和少量有明确生命周期的缓存，不能笼统标记为无状态。阶段 3 已将 attachment delivery 收入 `ContextSession`；builder 命名与其余缓存拆分留在阶段 5。

- `ContextBuilder`：无状态地完成 conversation → model 投影、规范化、预算和裁剪。
- `ContextSession`：每个活动 session 一个，记录已交付的 live-session attachments；锚点离开 working set 时裁剪，resume 或切换时销毁。
- user context / prompt stable cache：按其声明的 runtime、session 或 request 生命周期缓存；session 级缓存随 `ContextSession` 重建。
- microcompact proposal 和 `ContextBudget`：单次 build 状态。proposal 必须由 `Session` 持久化后，才可用于实际模型请求。
- full compact：`context` 生成候选 summary、replacement 和 boundary，`Session` 负责按固定顺序提交；摘要失败不得改变会话。

Todo 等 feature 状态优先从完整 Conversation history 投影，不再维护第二份可变列表。Todo reminder delivery 属于 `ContextSession`，Todo 事实仍来自已提交的 ToolResult。

## 模块状态表

| 模块 | 状态性质 | 生命周期与销毁条件 |
| --- | --- | --- |
| `model` | 无运行时状态 | 仅不可变 DTO、事件和 `ModelClient` 能力 |
| `conversation` | 有状态 | 一个 `Conversation` 对应一个已打开 Session；切换时整体替换 |
| `sessions` | 有状态且可持久化 | Transcript 跨进程；`Session` 与 store 索引随打开的 session 存活 |
| `context` | 部分有状态 | request 数据用完即弃；`ContextSession` 随活动 session；稳定缓存按声明失效 |
| `agent` | turn 内有状态 | step、usage、流式块和取消清理只活到一次 turn 结束；turn 之间不保存会话 |
| `tools` | 运行时配置为主 | Registry 在 bootstrap 后只读；ToolExecutor 持有 Workspace 与 runtime policy；执行状态属于单次调用；外置结果跟随 session 持久化 |
| `permissions` | 有状态 | 当前 runtime 的 mode/rules 可变；持久规则由 `config` 落盘；结构化 request/decision 属于单次调用，pending approval 完成或取消即销毁 |
| `providers` | 有状态 | 活动连接、SDK client、model capabilities 和切换锁活到 runtime 关闭或 provider 切换 |
| `prompts` | 部分有状态 | static/runtime、session、request 三种缓存必须分别失效 |
| `features` | 默认无独立状态 | Todos 等从 Conversation 投影；确需状态时必须在 feature 内声明生命周期 |
| `chat` | 有状态 | 拥有当前活动 Session 引用、ContextSession、session/turn 互斥和前端 permission handler |
| `workspace` | 无会话状态 | 路径与 I/O service 可复用，操作状态只活到一次调用 |
| `config`、`auth` | 可持久化 | 文件跨进程；解析后的快照在显式更新时替换 |
| `tui`、`cli` | host 状态 | TUI widget/task 随界面；CLI 参数随进程，不成为核心事实 |

“无状态”表示模块不在两次业务调用之间拥有可变领域事实，不表示对象不能持有不可变配置或依赖引用。

## Agent 与 Chat 的边界

`AgentEngine` 是无状态执行内核。`run/stream` 接收当前 `Session` 和对应的 context 状态，局部维护一次 turn 的 step、token usage、流式序列和工具轮。取消时必须闭合并提交已经持久化的 tool calls，随后丢弃 turn 状态。

`chat` 是长生命周期协调者，负责：

- 串行化 submit、compact 与 resume，禁止流式响应中途切换 session；
- 持有并原子替换活动 Session bundle；
- 把 Agent 事件投影成 host 事件；
- 协调 provider 切换与权限确认，但不拥有其内部状态。

因此 resume、session catalog 和 UI history projection 不属于 `agent`。

## 现有能力验收

状态设计至少要保持以下行为：

- 用户消息在首次模型调用前落盘；完整 assistant 响应在工具执行前落盘；
- 工具取消、异常或进程中断后，恢复时能够补齐协议所需的错误结果；
- resume 严格校验目标日志，失败不污染当前 session；
- microcompact 与 full compact 可持久化并重建同一 working set；
- live-session attachments 跨当前进程内的后续请求重放，但 resume 后不重放；
- Todo 从完整会话事实恢复，reminder delivery 不进入 Transcript；
- 历史工具展示优先使用持久快照，旧记录可退回当前 Tool 投影；
- provider 切换不改写历史，binding 不匹配的 continuation 不回放；
- session 切换与 turn/compact 互斥，TUI 不直接持有核心消息或读取 JSONL。
