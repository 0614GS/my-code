# 上下文管理

## 1. 上下文是请求时规范化

完整会话历史不会原样发送给模型。每次循环迭代都会从当前消息重新构造模型可见上下文：

```text
完整消息历史
  → 取最后一个 compact boundary 之后的消息
  → 限制聚合工具结果大小
  → 可选 snip / microcompact / context collapse
  → 必要时 auto compact
  → 注入 user context、system context
  → normalizeMessagesForAPI
```

主流程位于 `claude-code/src/query.ts:331` 附近。完整历史负责恢复和 UI；规范化视图负责 token、协议合法性与 prompt cache。

## 2. 请求的组成

模型上下文不只有对话消息，还包括：

- system prompt；
- user context，例如 cwd、代码库信息和记忆；
- system context；
- 工具描述和 JSON Schema；
- 对话消息、tool result 与 attachment；
- 为 thinking 和本轮输出预留的 token。

`fetchSystemPromptParts()` 并行构造稳定的 prompt 前缀，见 `claude-code/src/utils/queryContext.ts`。自定义 system prompt 是替换默认 prompt，append prompt 才是追加。

## 3. Token 计量

阈值判断的规范入口是 `tokenCountWithEstimation()`：

1. 从后向前寻找最近一次真实 API usage。
2. 以该次 `input + cache creation + cache read + output` 作为已知上下文大小。
3. 对该响应之后新增、尚未计入 usage 的消息做粗略估算。
4. 并行工具把同一 API 响应拆成多个 assistant 记录时，回退到第一个同 `message.id` 的片段，确保中间所有 tool result 都被估算。

源码入口为 `claude-code/src/utils/tokens.ts:226`。

不能把历次 usage 相加：输入上下文在每轮重复出现，累计会严重重复计数；也不能只看最近的 `output_tokens`，它不包含已有上下文。

## 4. 第一层：大结果外置

工具可以声明 `maxResultSizeChars`。单个结果超过阈值时：

1. 完整文本写入当前 session 的 `tool-results/`。
2. 模型只收到文件路径、大小和前部预览。
3. 非文本内容不使用这条持久化路径。

`Read` 的阈值可设为 `Infinity`，因为把 Read 输出保存成文件再让模型 Read 会形成循环；Read 自身通过读取上限控制大小。

除此之外还有“API 级 user 消息的聚合预算”。并行工具的多个结果最终会被合并为一个 user 消息，因此预算按规范化后的消息组计算，而不是按内部消息逐条计算。超限时优先外置最大的、尚未发送过的结果，直到回到预算内。

每个 tool ID 的“保留全文或替换预览”决策会冻结并写入 Transcript。恢复会话时重放完全相同的替换字符串，避免旧 prompt 前缀发生字节变化。

实现位于 `claude-code/src/utils/toolResultStorage.ts`。

## 5. 第二层：Microcompact

microcompact 只处理可丢弃的旧工具结果，典型对象是 Read、Bash、Grep、Glob、WebSearch 和 WebFetch。

参考快照有两条主要路径：

- **缓存仍热**：通过 cache editing 删除旧 tool result，本地消息不变；API 返回后再根据真实删除 token 写 microcompact boundary。
- **缓存已冷**：距离上次 assistant 响应超过阈值时，直接把较老结果替换为固定清除标记，至少保留最近一个结果。

固定替换文本和稳定 tool ID 顺序使操作可重复。microcompact 不负责总结任务状态；如果清理后仍接近上限，交给完整 compact。

实现位于 `claude-code/src/services/compact/microCompact.ts`。

## 6. 第三层：完整 Compact

有效上下文窗口先扣除摘要输出预留，再设置自动压缩阈值。当前快照中摘要最多预留 20k token，auto compact 又保留 13k buffer；这些是策略参数，不应写死在 Agent Loop。

完整 compact 流程：

1. 运行 PreCompact hooks，合并用户和 hook 给出的摘要要求。
2. 用独立模型调用总结当前对话；尽量共享主会话 prompt cache 前缀。
3. 如果摘要调用本身 prompt-too-long，按完整 API round 从头丢弃旧组后重试，而不是任意切断消息。
4. 保存并清空 read-file cache。
5. 生成 compact boundary 和一条标记为 compact summary 的 user 消息。
6. 恢复仍需直接可见的状态。
7. 运行 SessionStart/PostCompact hooks。

compact 结果的固定顺序是：

```text
boundary
→ summaryMessages
→ messagesToKeep（可选）
→ attachments
→ hookResults
```

定义见 `buildPostCompactMessages()`，位于 `claude-code/src/services/compact/compact.ts:330`。

## 7. Compact 后重建工作集

摘要不能可靠保存所有操作性状态，因此 compact 后会重新注入：

- 最近读取的文件，默认最多 5 个；每文件和总 token 均有限额；
- 当前 plan 文件和 plan mode 指令；
- 本会话实际调用过的 skill；
- 尚未回收结果的异步 Agent 状态；
- deferred tools、agents 和 MCP instructions 的当前集合。

恢复文件时重新调用 Read 逻辑，而不是盲用旧缓存，从而重新经过路径验证并取得最新内容。已在 preserved tail 中存在完整 Read 结果的文件不会重复注入。

对应实现从 `createPostCompactFileAttachments()` 开始，位于 `claude-code/src/services/compact/compact.ts:1415`。

## 8. 边界与 Prompt Cache

上下文管理同时优化 token 和 cache 稳定性：

- system prompt 与工具列表保持确定性顺序。
- 已经发送过的工具结果不在后续轮次临时改变替换策略。
- 提供给 hooks/UI 的派生 tool input 使用克隆，不修改重新发给 API 的原始输入。
- compact boundary 记录已发现的 deferred tools，摘要后仍继续发送其 schema。
- preserved tail 的旧 usage 在恢复时清零，避免用压缩前 token 数触发重压缩。

“节省更多 token”不一定是更优策略；如果改变稳定前缀导致大段 cache 重建，成本和延迟可能更高。

## 9. 失败保护

- compact 和 session-memory 自身不会再次 auto compact，防止递归。
- auto compact 连续失败达到阈值后熔断；当前快照阈值为 3。
- 自动压缩关闭时，接近真实窗口上限会硬阻止请求，并为手动 compact 预留空间。
- API 实际返回 prompt-too-long 时还有单次 reactive compact 兜底。
- Stop hooks 不处理 prompt-too-long 等无有效模型回答的错误。

## 10. 核心不变量

1. 裁剪单位必须保持完整 API round 和 tool-use/tool-result 配对。
2. token 阈值包含 prompt、工具 schema 与输出预留，不只统计聊天文本。
3. compact 必须产生显式 boundary，才能正确规范化、持久化和恢复。
4. 摘要后要重建工作集，不能假设摘要等价于原始操作上下文。
5. 同一历史前缀的替换决策必须可重放且字节稳定。

## 11. nano-code 的三层实现

nano-code 现在显式区分三种数据形态：

```text
SessionStore / JSONL（完整事实）
  → AgentEngine working set（最后一个 compact summary 之后）
  → ContextPlanner / ContextPlan（单次模型请求）
```

`TranscriptMessage` 保存 UUID、父链、origin、时间戳和 assistant usage；
`ModelInputMessage` 只保留 role 与模型可见 content。`ContextPlanner` 从不可变
`ConversationSnapshot` 生成 prompt sections、稳定工具顺序、规范化消息和
`ContextBudget`，Agent Loop 不再直接拼装请求。相邻同 role 消息在规范化时合并，
重复、孤立或未闭合的工具调用在进入 Provider 前失败。

预算优先以最近一次 assistant 的真实 input、cache creation/read 与 output usage
为锚点，只对
其后新增消息作字符估算；没有 usage 的旧会话才估算完整 system、tool schema 和
messages。`contextChars` 当前仍是触发 compact 的保守字符阈值，不冒充精确的模型
token 上限，`/context` 会同时显示字符组成和 token 估算。

microcompact 只处理旧的 Read、Bash、Grep、Glob 结果。原文不改写，JSONL 追加
`content_replacement`，每次按 tool ID 重放同一占位文本。完整 compact 使用独立、
无工具的模型请求；摘要成功后依次追加 `compact_boundary` 和 summary message，随后
仅释放内存中的旧工作集。摘要失败不会产生有效 boundary。Provider 明确报告上下文
超限时最多执行一次 reactive compact。

当前有意未实现 cached microcompact、preserved tail、文件/plan/skill 工作集重建和
compact hooks。这些能力以后应接入现有 planner、boundary 和 replay 边界，而不是
重新进入 Agent Loop 添加工具特定分支。

## 12. 主要源码入口

- `claude-code/src/query.ts`
- `claude-code/src/utils/{context,tokens,toolResultStorage,messages}.ts`
- `claude-code/src/services/compact/{autoCompact,microCompact,compact,grouping}.ts`
- `claude-code/src/utils/queryContext.ts`
- `claude-code/src/services/api/claude.ts`

## 13. ContextPort 与压缩协调

AgentEngine 只依赖 `ContextPort` 的四个能力：`plan`、`inspect`、
`compaction_view` 和 `measure`。默认实现仍是 `ContextPlanner`；窗口、提示词、工具
schema、microcompact 和 API 规范化都留在 context 包内，Engine 不读取其 `window` 等
内部对象。

完整 compact 由 `CompactionCoordinator` 实现 `Compactor`：它请求
`ContextPort.compaction_view()`，调用 `CompactionService` 生成摘要，构造 summary
message 和 boundary，并返回尚未写入的 `CompactionOutcome`。实际追加和工作集替换
由 `ConversationState` 完成，避免摘要生成失败或持久化失败时提前改变会话内存。
