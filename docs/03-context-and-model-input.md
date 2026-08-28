# Context 与模型输入

`context` 把不可变 Session snapshot 转换为一次 provider-neutral `ModelRequest`。它负责投影、预算和 compact proposal，但不拥有或修改对话事实。

## 从事实到请求

```text
ContextPlanningState
  + ContextRuntime cache
  + PromptRegistry
  + Tool definitions
  + ActiveModelEnvironment
        -> ContextEngine / ContextPlanner
        -> SystemPrompt + ordered ModelInputItem[]
        -> ModelRequest
        -> Provider adapter wire payload
```

`ModelRequest.input` 是有序的语义联合：

- `UserInput`：真实用户输入、用户上下文、summary 或 Attachment 的投影。
- `AssistantOutput`：已完成 step 的 text、reasoning 和 ToolCall。
- `ToolOutputs`：与前两者并列的工具结果，不伪装为 user message。

公共模型支持 text、image、document、reasoning、tool call 和 tool output，但不包含 OpenAI/Anthropic SDK 类型。Context 保留语义顺序，不执行 Provider role 合并。

## Provider 映射

| 公共输入 | Anthropic Messages | OpenAI Responses |
| --- | --- | --- |
| `UserInput` | user content | message input item |
| assistant text/reasoning | assistant blocks | message/reasoning items |
| ToolCall | assistant `tool_use` | `function_call` item |
| `ToolOutputs` | user-role `tool_result` blocks | 顶层 `function_call_output` items |

Anthropic adapter 才负责相邻 role 归一化和 tool result 的 user-role 包装；OpenAI adapter 直接输出顶层 function items。两者都用 provider-neutral `call_id` 配对调用和结果，Provider 自身的 response item ID 只存在于 replay payload。

Provider adapter 只消费 `ModelRequest`，不读取 Session 或 AppState。绑定不匹配时保留 canonical content，但不重放 opaque continuation；compact 后已离开工作集的 replay 也不会进入请求。

## Prompt、用户上下文与 Attachment

`prompts` 拥有 system prompt section 的内容、顺序和稳定性声明。`PromptRegistry` 把 section 解析为 `SystemPrompt`；Provider 根据能力映射 cache control，但 prompt 模块不依赖具体 SDK。

`ContextRuntime` 的生命周期与一个活动 Session 或 child run 完全一致，只缓存：

- session-stable prompt section；
- 首次解析后的 AGENTS 用户上下文。

替换 Session 或创建 child run 会创建新的 ContextRuntime；Provider/模型切换不会自动丢弃与 Session 绑定的 cache。

显式 `@path`、Todo reminder、Skill 信息和后台完成结果先成为有序 `AttachmentMessage`，再由纯 projector 原位转换为 `UserInput`。已激活 Skill 不修改 system prompt。用户上下文是请求时可信输入，不写入 Conversation，也不作为恢复历史展示。

## 预算与 Compact

`ContextBudget` 综合字符规模、token 估算、Provider usage 校准、输出预留和当前模型限制。超限处理依次为：

1. 对旧的大型工具结果建立 content replacement。
2. 仍超限时生成 full compact proposal。
3. Provider 返回 context overflow 时允许一次 reactive compact。
4. 仍无法构造合法请求时返回明确错误。

`ContextEngine.compact()` 只生成 `CompactionOutcome`；Agent 的自动/反应式路径或 Chat 的手动路径再通过 `Session.commit_compaction()` 原子提交 summary、replacement 和 boundary。摘要失败、截断或重试耗尽时 Session 不改变。

Compact 请求显式关闭 reasoning，并使用受模型上限约束的独立输出预算。超长 compact 输入可以按完整用户轮次从最旧部分开始裁剪摘要视图，但不会删除完整 transcript。最终 summary 是新的 Conversation fact，而不是隐藏 cache。

## 必须保持的不变量

- 相同 planning state、runtime cache、工具定义和模型环境产生确定性请求计划。
- Context 不提交 Session、不缓存第二份 history，也不解释 JSONL。
- Attachment projector 只能产生 user input，不能制造 assistant 或 tool 协议项。
- ToolCall 与 ToolOutput 的配对不能被预算或注入拆开。
- Provider role/wire normalization 只存在于对应 adapter。
- compact proposal 只有经 Session 原子提交后才能成为后续请求事实。

主要源码入口：`src/my_code/context/engine.py`、`src/my_code/context/planner.py`、`src/my_code/context/session.py`、`src/my_code/model/request.py`、`src/my_code/prompts/registry.py`。
