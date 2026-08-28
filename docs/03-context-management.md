# 上下文管理

## 职责

`context` 把不可变 `ContextPlanningState` 转换为一次 provider-neutral `ModelRequest`。它计算请求预算、规范化、工具结果替换、attachment 和 compact proposal，但不拥有或修改 Session 状态。

主要模块：

- `context.engine`：模块对外的 plan、inspect 和 compact 能力。
- `context.planner`：确定性地构造请求与提交前决策。
- `context.session`：最小规划/attachment 输入与非持久化 `ContextRuntime`。
- `context.models`：预算、计划与压缩结果。
- `context.compaction`：摘要模型调用与 compact proposal。
- `context.attachments`：Conversation attachment payload 到 `UserInput` 的纯投影与动态 source 协议。

## 请求组成

```text
SystemPrompt
+ User context
+ Conversation context entries
+ ordered AttachmentMessage entries
+ Tool definitions
=> ModelRequest
```

Context 会把 Conversation entries 转换为有序 `ModelInputItem`，不会修改 canonical facts。动态 attachment source 先由 Agent 解析并交给 Session，随后 Context 对新的 planning state 进行纯规划。`ContextPlan` 中只有 content replacement 等请求决策；相同 state、runtime cache 与 model environment 产生确定性 plan。

## 预算

`ContextBudget` 同时记录字符规模、tokenizer 估算、provider 上报校准、输出预留和模型限制来源。触发阈值来自当前 `ActiveModelEnvironment`；provider 切换只在模型调用之间原子更新该环境。

超限顺序：

1. 对旧的大型工具结果建立 content replacement。
2. 若仍超过阈值，执行完整 compact。
3. provider 返回 context overflow 时允许一次 reactive compact。
4. 仍无法构造请求则抛出 `ContextOverflow` 或 `ModelContextOverflow`。

## Compact

`ContextEngine.compact()` 调用内部 `ContextCompactor` 并只返回 `CompactionOutcome`，不直接写 Session。Agent 在 auto/reactive 恢复路径提交 outcome；Chat 在 manual compact 用例中提交。两者都通过 `Session.commit_compaction()` 一次写入 replacements、summary 和 boundary。

摘要不是隐藏缓存，而是新的 `ConversationSummaryMessage`。恢复时 Session 根据私有 compact boundary 派生 `context_entries`，因此 compact 后仍可继续对话。

Compact 使用独立的内部输出策略：默认请求 20,000 tokens，若已知模型声明了更低的输出上限则收窄到该上限，并显式关闭该请求的 reasoning。摘要契约是非空的结构化 Markdown handoff；解析层会清除完整的 `analysis`/`analyze` 草稿块，并兼容解包旧的 `summary` 标签和整段 Markdown fence。

输出因 `max_tokens` 或 `max_output_tokens` 截断时，未完成内容不会进入 proposal；同一输入最多重试一次，重试提示要求完整内容不超过 16,000 tokens。若 compact 输入本身被 provider 拒绝为超长，则只对本次摘要视图执行有损重试：以 `UserInput` 开始的完整轮次为边界，每次移除当前最老的 20%（至少一轮），保留最新轮次并加入较早上下文省略标记，初次请求之外最多裁剪重试三次。该裁剪不删除完整 transcript。

一次成功 compact 中所有已完成模型调用的 token usage 会累计到 `CompactionOutcome.usage`；只有每次调用都由 provider 上报时，累计值的 `provider_reported` 才为真。任意重试全部失败时不会返回 proposal，因此 replacements、summary、boundary 与 Session 均不推进；只有最终成功后才执行上述原子提交。

## Session context state 生命周期

每个活动 Session 配对一个独立 `ContextRuntime`，每个 subagent run 也独占一个。它持有：

- 首次解析后复用的用户上下文。
- 当前 session 的稳定 prompt section cache。

provider 切换不重建 Runtime；Session replace 和新 child run 必须重建。Todo reminder 从完整 `conversation` 投影当前 Todo、从 `context_entries` 计算提醒间隔；Skill listing 按后缀去重；后台通知用 session ID 与完整 conversation 去重。
