# 提示词管理

## 1. 职责边界

nano-code 不把提示词当成 Agent Loop 中的一段常量字符串。模型请求经过以下边界：

```text
PromptSection 来源
  → PromptRegistry 解析与生命周期缓存
  → SystemPrompt 请求值对象
  → ContextPlanner 组合消息、工具和预算
  → Provider 映射协议字段与服务端缓存
```

`prompts/` 只描述提示词内容、顺序和稳定性，不依赖 Anthropic SDK。
`ContextPlanner` 负责形成单次请求，但不理解具体提示词文本。Provider 只消费已经
解析的片段，不参与提示词业务内容的选择。

## 2. 分段与稳定性

每个 `PromptSection` 有稳定 key、延迟 resolver 和一种生命周期：

- `STATIC`：与会话无关的身份、工具原则和安全边界；
- `SESSION`：当前工作区等会话内不变的信息；
- `TURN`：需要随每次模型请求重新计算的信息。

Registry 强制按 static、session、turn 排列，并拒绝重复 key。static/session 首次解析
后在当前 Registry 中冻结，turn 每轮重新解析。这个规则既防止无意改变已发送前缀，
也为 provider prompt cache 保留稳定断点。

## 3. Provider 缓存

核心只提供稳定性，不生成 `cache_control`。`ProviderCapabilities` 由适配器声明，
provider 决定是否把稳定边界转换为协议缓存点。官方 Anthropic endpoint 当前声明支持
两个 system prompt 缓存断点；自定义 Anthropic-compatible URL 保守回退为普通文本。

缓存能力不是用户偏好，因此不进入 settings、provider profile 或 TUI。未来 provider
可以采用不同缓存协议，而无需修改 PromptRegistry 或 Agent Loop。

## 4. XML 上下文块

compact summary 和类似 reminder 的内部说明使用 `SystemContextBlock` 保存。Transcript
记录结构化 kind 与 content；`ModelMessageProjector` 到请求边界才渲染
`<conversation-summary>` 或 `<system-reminder>`。

XML 只是帮助模型识别语义的文本协议，不是可信执行通道。普通用户和工具输出中的同名
标签不会升级成内部上下文块；可信性来自消息 origin、强类型构造和统一投影路径。

## 5. 后续扩展

项目记忆、hooks、skills 和动态工具说明应作为新的 prompt source 或系统上下文来源
接入。它们不应直接修改 Agent Loop，也不应在 Provider 中拼装业务提示词。
