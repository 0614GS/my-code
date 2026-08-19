# 上下文管理

## 职责

`context` 把不可变 Session snapshot 转换为一次 provider-neutral `ModelRequest`。它计算请求预算、规范化、工具结果替换、attachment 和 compact proposal，但不拥有或修改 Session 状态。

主要模块：

- `context.engine`：模块对外的 plan、inspect 和 compact 能力。
- `context.planner`：确定性地构造请求与提交前决策。
- `context.session`：不可变 context view 与 Session 的窄访问协议。
- `context.models`：预算、计划与压缩结果。
- `context.compaction`：摘要模型调用与 compact proposal。
- `context.attachments`：不写 Transcript 的临时上下文。

## 请求组成

```text
SystemPrompt
+ User context
+ Conversation working set
+ request/live-session attachments
+ Tool definitions
=> ModelRequest
```

Context 会把 Conversation entries 转换为有序 `ModelInputItem`，不会修改 canonical facts。`ContextPlan` 中明确列出的 content replacement 或 attachment delivery 由 Agent 在请求使用前交给 Session 原子提交。相同 snapshot、session cache 与 model environment 产生确定性 plan。

## 预算

`ContextBudget` 同时记录字符规模、tokenizer 估算、provider 上报校准、输出预留和模型限制来源。触发阈值来自当前 `ActiveModelEnvironment`；provider 切换只在模型调用之间原子更新该环境。

超限顺序：

1. 对旧的大型工具结果建立 content replacement。
2. 若仍超过阈值，执行完整 compact。
3. provider 返回 context overflow 时允许一次 reactive compact。
4. 仍无法构造请求则抛出 `ContextOverflow` 或 `ModelContextOverflow`。

## Compact

`ContextEngine.compact()` 调用内部 `ContextCompactor` 并只返回 `CompactionOutcome`，不直接写 Session。Agent 在 auto/reactive 恢复路径提交 outcome；Chat 在 manual compact 用例中提交。两者都通过 `Session.commit_compaction()` 一次写入 replacements、summary 和 boundary。

摘要不是隐藏缓存，而是新的 `ConversationSummaryMessage`。恢复时 Session 根据 compact boundary 重建工作集，因此 compact 后仍可继续对话。

## Session context state 生命周期

每个活动 Session 私有持有：

- 已交付 attachment 及其 anchor。
- 首次解析后复用的用户上下文。
- 当前 session 的稳定 prompt section cache。

这些状态不由 ContextEngine 缓存，也不写 canonical transcript。切换或恢复会创建新的 Session，因此不能跨 Session 复用；Todo reminder 也是 attachment delivery，不是对话事实。
