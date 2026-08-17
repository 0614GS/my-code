# 提示词管理

## 1. 职责边界

nano-code 不把提示词当成 Agent Loop 中的一段常量字符串。模型请求经过以下边界：

```text
constants/prompts.py 的静态文本
  → prompts/system.py 组装 PromptSection
  → PromptRegistry 解析与生命周期缓存
  → SystemPrompt 请求值对象
  → ContextPlanner 组合消息、工具和预算
  → Provider 映射协议字段与服务端缓存
```

`constants/prompts.py` 只保存稳定的英文默认文本，静态内容不读取 cwd 或其他工作区
变量。当前默认静态片段是 identity、system、task guidance、safety、tools 和
response style；它们只描述 nano-code 已实现的行为和工具。
`prompts/` 负责提示词内容的组装、顺序和稳定性，不依赖 Anthropic SDK。
`ContextPlanner` 负责形成单次请求，但不理解具体提示词文本。Provider 只消费已经
解析的片段，不参与提示词业务内容的选择。

## 2. 分段与稳定性

每个 `PromptSection` 有稳定 key、延迟 resolver 和一种生命周期：

- `STATIC`：与会话无关的身份、工具原则和安全边界；
- `SESSION`：当前工作区等会话内不变的信息；
- `REQUEST`：需要随每次 `ModelRequest` 重新计算的信息。

Registry 强制按 static、session、request 排列，并拒绝重复 key。static/session 首次解析
后在当前 Registry 中冻结，request 每次重新解析。这个规则既防止无意改变已发送前缀，
也为 provider prompt cache 保留稳定断点。

默认的 `nano-code.environment` session section 按固定顺序包含当前绝对工作区路径、
工作区自身是否有 `.git` 目录或 worktree `.git` 文件、平台、shell，以及 OS 类型和
版本。Git 判断不会向父目录遍历，也不执行 git 命令；不会把 git status 或其他每轮
变化的数据放入 prompt。

## 3. Provider 缓存

核心只提供稳定性，不生成 `cache_control`。`ProviderCapabilities` 由适配器声明，
provider 决定是否把稳定边界转换为协议缓存点。官方 Anthropic endpoint 当前声明支持
两个 system prompt 缓存断点；自定义 Anthropic-compatible URL 保守回退为普通文本。

缓存能力不是用户偏好，因此不进入 settings、provider profile 或 TUI。未来 provider
可以采用不同缓存协议，而无需修改 PromptRegistry 或 Agent Loop。

## 4. User context 与 attachments

`UserContextResolver` 是独立于 provider 的上下文来源接口，返回一组未渲染的
`UserContextDocument`。文档带有来源名和 `TextContent | ContextInstruction` 内容，但没有
conversation UUID、父链或时间戳。`ContextPlanner` 在自己的生命周期内缓存 user context，
并通过 `ModelInputNormalizer` 将其直接放入最终 `ModelRequest.messages`；预算统计使用规范化后
的模型可见文本，因此包含 XML wrapper 的字符量。

Anthropic 请求的顺序是 `user_context → conversation messages / anchored deliveries → current
derived attachments`。AGENTS context
不是 Transcript 事实，不会写入 JSONL，也不会进入 `compaction_view()` 或 compact summary。当前组合根显式
装配 `AgentsUserContextResolver`，只读取 `AgentSettings.cwd / AGENTS.md`。非空文件会
作为一条前置 user 消息注入，并使用 CC 风格的 `<system-reminder>` 包裹，包含
`# AGENTS.md` 来源标识和“仅在相关时使用”的提醒。

文件缺失或为空时不注入 context；文件存在但无法读取或不是有效 UTF-8 时显式失败。
resolver 不遍历父目录，不读取 `CLAUDE.md`、`.claude/` 或规则目录，也不解析 include。

`DerivedAttachmentResolver` 是同步、无状态的聚合器，接收按声明顺序排列的
`DerivedAttachmentSource`。每个 source 都收到当前 `ConversationSnapshot`，返回一组
`ContextAttachment`；每次请求都会重新执行 source，不做 session 缓存。source 的结果会先
整体收集，source 失败时记录异常并跳过该 source，其他 source 仍然生效。无 source 的
resolver 返回空 tuple。

`ContextAttachment.retention` 区分仅当次可见的 `request` 和本进程会话可见的
`live_session`。后者会以 `AttachmentDelivery` 锚定到 working-set message，按原位置
参与后续请求与 compact 输入，但不写 Transcript。`AgentTurnInput` 则是回合事件
attachment 的入口；这条路径与基于快照的 derived source 分开，为后续 TUI `@`
文件选择保留语义边界。

attachment content 支持 `TextContent`、`ContextInstruction` 和受信任的
`AttachmentToolExchange`。前两者投影为 user text/reminder；后者投影为配对的
assistant tool-use 与 user tool-result，并与真实会话一起经过全局 tool ID 配对校验。
这些投影都由 `AttachmentProjector` 集中完成，不会伪装成
`ConversationMessage`。

## 5. XML 上下文块

compact summary 使用独立 `ConversationSummaryMessage`；请求级 reminder 使用
`ContextInstruction`。XML wrapper 集中位于 `messages/xml.py`，并由
`ModelInputNormalizer` 到请求边界才渲染 `<conversation-summary>` 或
`<system-reminder>`。

XML 只是帮助模型识别语义的文本协议，不是可信执行通道。普通用户和工具输出中的同名
标签不会升级成内部上下文块；可信性来自判别联合和统一规范化路径。

## 6. 后续扩展

项目记忆、hooks、skills 和动态工具说明应按其生命周期作为新的 prompt source、
derived attachment source 或事件 attachment 接入。它们不应直接修改 Agent
Loop，也不应在 Provider 中拼装业务提示词。
