# 消息与会话

## 1. 三层消息边界

nano-code 不在运行时消息上保存 API role。消息沿着单向边界逐层收窄：

```text
TranscriptEntry（JSONL recovery record）
  → ConversationMessage（启动/resume hydration）
  → ConversationState（live session 事实）
  → ModelMessage / ModelRequest（模型协议）
  → provider wire type

ConversationState ──追加持久化──→ TranscriptEntry
```

`ConversationMessage` 是按语义判别的联合：`HumanMessage`、`AssistantMessage`、
`ToolResultsMessage` 和 `ConversationSummaryMessage`。只有 assistant 能包含
`TextContent | ToolCall | ReasoningContent` 并且必须携带 `TokenUsage`。reasoning
把可展示的 `ReasoningPresentation` 与隐藏的 `ProviderContinuationState` 分开；后者也可挂在 text/tool call 上以保真保存 OpenAI output item。只有 tool-results 能包含带本地
`presentation` 的 `ToolResult`。因此非法的 role/origin/content 组合无法构造。

`ContextPlanner` 是唯一的 conversation → model 投影边界。它把四种会话消息转换为
`ModelUserMessage | ModelAssistantMessage`，移除 UUID、父链、时间戳和 presentation，
合并相邻同 role 消息并校验 tool-use/result 配对。Provider 只接收完成的
`ModelRequest`，不理解 Transcript 或请求上下文来源。

nano-code 将非 Transcript 输入分成 `UserContextDocument` 和 `ContextAttachment`：前者
在会话生命周期内缓存并放在历史之前；后者是带明确 retention 的模型可见附加上下文。
`ContextAttachment` 可包含文本、结构化 reminder 或 provider-neutral 的
`ContextObservation`，但不进入 JSONL 或父链；它始终投影到 user side，不能产生
assistant/tool 协议块。

`request` attachment 只对当前模型请求有效。`live_session` attachment 会以
`AttachmentDelivery` 锚定到当前 working set 中的一条消息，在本进程的后续请求
和 compact 输入中保持原位置；锚点被 compact 移出 working set 后交付随之裁剪。
delivery 不持久化，因此 resume 或切换 session 后不会重放。

## 3. 身份与流式顺序

源码刻意分开几种不同用途：

- `Message.uuid` 是本地 Transcript 记录和父链身份。
- `ReasoningContent.id` 是 reasoning 完成后生成的本地持久化身份；实时事件不携带它。
- `tool_use.id` 是工具协议关联键，`tool_result.tool_use_id` 必须与之相同。
- Anthropic message ID、OpenAI response/item ID 等原始身份只存在于匹配 provider 的
  continuation payload，不进入 Application/TUI。
- Model stream `sequence_number` 只表示单次请求内规范化事件的先后顺序，每次请求
  重新计数，不能替代上述任何身份。

因此流式展示不依赖 provider ID 或 UUID 去重。临时块在 completed 到达时原子收口，
最终 `ModelOutput` 只写入一次 Conversation/Transcript。

## 4. JSONL Transcript schema

主会话按项目目录和 session ID 写入 JSONL；子 Agent 写入独立 sidechain 文件。核心实现位于 `claude-code/src/utils/sessionStorage.ts`。nano-code 的对应磁盘布局见 [09-storage-and-settings.md](09-storage-and-settings.md)。

当前 schema 是 breaking version 5，每行都有 `schema_version: 5`。第一条必须是 `session_started`，记录 Session ID、canonical cwd、创建时间和初始 provider/model/permission/agent 限制快照，其中 Step 安全阀使用 `max_steps`。后续联合 discriminator 为：

- `human_message`、`assistant_message`、`tool_results_message`、`conversation_summary_message`；
- `session_metadata`、`content_replacement`、`compact_boundary`。

records 定义在 sessions adapter 内，严格 codec 是唯一同时依赖 `TranscriptEntry` 和
`ConversationMessage` 的模块。旧 `type: "message"`、未知 schema version、未知字段及非法
内容组合全部拒绝；不提供迁移或 fallback parser。SessionCatalog 会跳过这类旧候选，显式
resume 则报告带路径和重建提示的版本不兼容错误。v4 不迁移；v5 assistant content 使用严格的
`reasoning` record，并允许 text/tool-call 携带可选 continuation。presentation 明文保存，binding、scope 和 JSON payload 严格校验。只有 EOF 处没有换行且无法解析的中断尾记录可以忽略；完整坏行始终拒绝恢复。

## 5. 父链、分支与恢复

普通 Transcript 消息包含 `parentUuid`，恢复时从叶子向父节点回溯，再反转成对话顺序。

写入规则的关键点：

- 新消息默认以最近的 chain participant 为父节点。
- tool result 优先以 `sourceToolAssistantUUID` 为父节点。
- compact boundary 的 `parentUuid` 置空，表示新物理链起点；旧父节点写入 `logicalParentUuid` 供展示和诊断。
- 重复 UUID 不再次写入，防止每轮把完整内存数组重复追加。

这使 rewind、fork 和 sidechain 可以共享已有消息，而不复制成一条完全线性的日志。

## 6. 并行工具带来的 DAG

父链接近链表，但并行工具会形成 DAG：同一个模型响应的多个 assistant 片段拥有相同 `message.id`，各自的 tool result 又指向不同 assistant UUID。单纯沿一条 `parentUuid` 回溯可能丢掉兄弟分支。

`buildConversationChain()` 后会运行恢复遍历：按共享的 API message ID 收集离链 assistant 片段，再找出它们的 tool result，按时间顺序插回响应组。源码入口：

- `claude-code/src/utils/sessionStorage.ts:2040`
- `recoverOrphanedParallelToolResults()`

这说明持久化模型必须显式支持“一次响应有多个并列内容块”，不能假设所有事件天然是一条链。

## 7. Compact 后的恢复

compact boundary 表示模型上下文从这里重新开始，但 Transcript 仍可能保存被摘要的旧记录。加载时会：

1. 定位最后一个 boundary。
2. 删除 boundary 之前不再需要的消息。
3. 如果 compact 保留了一段原消息，依据 `preservedSegment` 把该段首尾重新接到摘要链上。
4. 清零保留 assistant 消息里的旧 usage，防止恢复后立即再次触发 compact。

如果 preserved segment 不完整，读取器宁可放弃裁剪、恢复更多旧历史，也不构造一条断链会话。

## 8. 核心不变量

1. API 投影、UI 事件和持久化记录是三个独立边界。
2. progress 不得成为可恢复对话的父节点。
3. 每个 tool result 必须能定位到 tool use 和产生它的 assistant 消息。
4. Transcript 写入必须按 UUID 幂等。
5. 恢复逻辑必须检测环、断链和并行工具的兄弟分支。

## 9. 主要源码入口

- `claude-code/src/types/message.ts`
- `claude-code/src/utils/messages.ts`
- `claude-code/src/utils/sessionStorage.ts`
- `claude-code/src/utils/sessionRestore.ts`
- `claude-code/src/assistant/sessionHistory.ts`

## 10. nano-code 的会话发现与恢复边界

nano-code 将“列出会话”和“恢复会话”分开处理：

1. `SessionCatalog` 只扫描当前项目对应的状态目录，通过固定大小的 head/tail 提取启动快照、首条用户提示和最后一条 metadata，不为列表加载完整 JSONL。
2. 显式 title 优先于首条有效 prompt；候选按 metadata 更新时间倒序排列。当前活动会话、空文件、异常首记录、非 UUID 文件和符号链接都会被过滤。
3. 用户选择后，`SessionStore` 才严格解析完整记录，校验 UUID 唯一性和父节点顺序，并从最后一条记录沿父指针恢复活动分支。
4. `AgentEngine.resume()` 先完成目标会话校验及未闭合工具轮修复，再替换内存状态；失败时保留原会话。
5. runtime 在同一临界区切换 transcript、消息历史和工具结果目录，TUI 只接收展示 DTO，不接触 `ConversationMessage` 或 JSONL。

这对应 Claude Code 的轻量日志列表、选中后完整加载、集中恢复 session-scoped 状态三层设计。nano-code 当前仍没有 fork 和并行工具兄弟分支，因此活动链算法保持更小的范围。

工具结果额外保存可选的 `presentation` 快照。它不是发送给 Anthropic Messages API
的内容，而是 Tool 在执行当时生成的前端无关摘要；恢复 UI 时优先复用它，从而避免
升级展示逻辑后历史会话突然改变。该字段在 schema version 5 中是可选字段。

Anthropic 的 `thinking`/`redacted_thinking` 和 OpenAI Responses 的 reasoning item 都保持
与 text/tool-use 的原始顺序。Anthropic continuation 只在活动工具轨迹回放；OpenAI output
item 在 compact 后工作集内回放。两者都要求 protocol、profile、model 和 endpoint binding
完全一致。Compact 视图删除 ReasoningContent，并从 text/tool block 剥离 continuation。

JSONL 还可追加两类上下文记录。`content_replacement` 按 tool ID 冻结模型可见的
microcompact 占位文本，原始工具结果保持不变；`compact_boundary` 记录压缩前父节点、
summary UUID、触发原因和压缩前字符量。boundary 先于 summary 写入，进程若在两次
追加之间退出，恢复器会忽略缺少 summary 的不完整边界。历史 UI 读取完整活动父链，
Agent 工作集则从最后一个有效 summary 开始。

## 11. 会话状态边界

`SessionRepository` 是 JSONL 实现向 Agent 暴露的最小接口：构造或 resume 时一次
`load()` 返回活动历史、compact 后工作集、metadata、内容替换和有效 boundary；追加消息、
替换和 boundary 仍是三个显式操作。`SessionStore` 负责解析和校验记录，
`ConversationState` 负责工作集、持久化优先追加、未闭合 tool-use 修复和会话切换。

`ConversationState` 是运行期会话事实来源。所有状态变更都遵循“先写 repository，
再将同一领域事实增量应用到内存”，不在写后重新解析 JSONL。恢复目标
会话时，目标日志的完整解析和修复完成前不会替换当前 repository；compact 提交则固定
按以下顺序执行：

```text
content replacements → compact boundary → summary message → working set replacement
```

Transcript v5 的 assistant message 可选保存 `provider_binding`、
`request_input_tokens_estimate`，usage 同时标记是否由 Provider 实际报告。这些字段不改变
schema 版本；旧记录缺失时不会把零 usage 当成事实，而是使用本地 tokenizer。session start
同时保存启动时解析出的 limits、来源和 compact threshold 诊断快照；恢复请求仍以当前
Profile、endpoint 与模型能力为准。

因此摘要模型失败不会写入 boundary，后续 JSONL 写入失败也不会让当前进程先使用一份
未持久化的工作集。`SessionSnapshot` 只是 hydration DTO；AgentEngine 不读取
SessionStore，后续 API 投影只依赖 `ConversationState.context_snapshot()`。
