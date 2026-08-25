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

## Session context state 生命周期

每个活动 Session 配对一个独立 `ContextRuntime`，每个 subagent run 也独占一个。它持有：

- 首次解析后复用的用户上下文。
- 当前 session 的稳定 prompt section cache。

provider 切换不重建 Runtime；Session replace 和新 child run 必须重建。Todo reminder 从完整 `conversation` 投影当前 Todo、从 `context_entries` 计算提醒间隔；Skill listing 按后缀去重；后台通知用 session ID 与完整 conversation 去重。
