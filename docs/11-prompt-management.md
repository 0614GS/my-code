# Prompt 管理

## 所有权

`prompts` 拥有 System prompt section 的内容、顺序和稳定性声明。`context` 负责在一次请求中组合 prompt、用户上下文和 attachments；Provider 只消费完成的 `SystemPrompt`。

## Prompt sections

`prompts.models.PromptSection` 声明 key、稳定性和 resolver。`PromptRegistry` 按注册顺序解析为 `ResolvedPromptSection`，再形成 `ModelRequest.system_prompt`。

runtime-stable section 在 `PromptRegistry` 构造时冻结；session-stable section 的缓存由当前 Session 私有持有。Provider 根据自身能力把稳定性映射为 prompt caching wire 字段；`prompts` 不依赖 Anthropic 或 OpenAI SDK。

已激活 Skill 不注册为 prompt section，也不会修改 system prompt。`Skill` Tool 成功后把正文作为 durable `SkillActivationAttachment` 放在完整 ToolResultBatch 之后；Context 在原位置将其投影为标准 `UserInput`。正文持续存在到 compact，compact 再按 Skill 名称生成 `InvokedSkillsAttachment`。未激活 Skill 的正文不会进入请求或 prompt cache。

## 用户上下文

`context.user_context.AgentsUserContextResolver` 从工作区层级读取适用的 AGENTS.md，转换为 `UserContextDocument`。解析结果在当前 Session 首次使用后缓存，resume/switch 时随新 Session 重建。

用户上下文与真实 HumanMessage 不同：它是请求时可信指令输入，不写入 Conversation，也不作为恢复历史展示。

## Attachments

显式 `@path`、todo reminder、后台完成通知和 Skill 上下文都转换为 `AttachmentMessage` payload，由 Session 排入 Conversation。Context normalizer 遍历有序 Conversation，并把每个 Attachment 原位投影为一个或多个 `UserInput`；Attachment projector 不能产生 assistant、tool call 或 tool result。

是否持久化完全由 Session 按 payload 类型决定，不由 producer 声明。动态 source 在每次模型请求规划前运行，Session 成功接受 payload 后才 acknowledge；幂等性来自 Conversation 中的 catalog version、task ID 或 reminder/model-call 间隔事实。

结构化 context block 的 XML 渲染集中在 `context.xml`。文件正文、目录列表和提醒不通过字符串拼接伪装成 ToolCall。

## 缓存边界

- Prompt 内容与 section 顺序归 `prompts`。
- Session 级 prompt 与 user-context cache 归 `Session` 私有 context state。
- Provider wire cache control 归 `providers`。
- Model capability 与限制归 `model.capabilities`。

这些缓存没有持久化为 Session facts，也不能在 provider 或 Session 切换后无条件复用。
