# 工具系统

## 模块职责

`tools` 拥有工具定义、注册、输入校验、权限调用、执行、展示投影和结果外置。内置文件与 Bash 工具位于 `tools.builtin`；TodoWrite 属于 `features.todos`，通过同一 Tool 接口注册。

## Tool 能力

具体 Tool 直接实现 `tools.base.Tool` 所需行为：

- 提供 provider-neutral `ModelToolDefinition`。
- 根据输入生成工具级权限判断。
- 在 `ToolContext` 中执行并返回 `ToolOutput`。
- 生成不含敏感原始数据的 presentation。

没有为单一实现建立额外 Tool port 或 adapter。

## 执行管线

```text
ToolCall
  -> ToolRegistry 查找与输入校验
  -> Tool 自身的权限分析
  -> PermissionPolicy 合并规则和 mode
  -> 必要时 PermissionPrompter 确认
  -> Tool 执行
  -> ToolResult + ToolResultPresentation
  -> 大结果按 session 写入 ToolResultStore
```

`ToolExecutor.execute()` 接受当前 session 的 `ToolResultStore`。Agent 路径必须显式传入 store，避免执行器隐藏绑定到某个活动会话。

## ToolRound

`tools.round_executor.ToolRoundExecutor` 串行执行同一 AssistantMessage 中的 ToolCall。它发送 started/finished 事件，并最终产生一条闭合的 ToolResultsMessage。

当前 MVP 不并行执行工具。取消时尚未执行完成的调用会得到稳定错误结果；已经完成的结果不会重复执行或丢失。

## 权限与工作区

- 输入含义和只读判断归具体 Tool。
- 全局 mode、allow/ask/deny 规则和确认顺序归 `permissions`。
- 路径解析、工作区逃逸防护和文件读写原语归 `workspace`。
- Bash AST、命令语义和重定向分析留在 `tools.builtin.bash`。

## 展示与持久化

模型可见 `ToolResult` 与前端使用的 presentation 是两份不同数据。Session JSONL 保存必要的 presentation 快照；大型原始输出可以写入 session 专属工具结果目录，但引用仍由 canonical ToolResult 维护。
