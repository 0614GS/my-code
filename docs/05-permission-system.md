# 权限系统

## 所有权

`permissions` 拥有规则表示、工具局部判断、全局决策顺序和用户确认协议。活动 `PermissionPolicy` 的唯一引用位于 `AppState.permissions`。`config.permission_updates` 负责把获批的长期更新写入设置，然后再更新这一个 policy。

## 决策数据

- `ToolPermissionResult`：工具根据具体输入得出的 allow/ask/deny/passthrough 事实。
- `PermissionRequest`：ToolExecutor 交给策略的输入。
- `PermissionDecision`：全局规则和 mode 合并后的可审计结论。
- `PermissionPrompt`：需要 host 确认时的安全展示数据。
- `PermissionConfirmation`：用户允许、拒绝或附带规则更新的响应。

这些结构处在不同阶段，即使字段相似也不能合并。

## 决策顺序

1. 显式整工具 deny。
2. 显式整工具 ask。
3. 工具级 deny。
4. 声明为 bypass-immune 的工具级 ask。
5. `bypassPermissions` 的宽松默认。
6. 显式整工具 allow。
7. 工具级 allow。
8. `dontAsk` 将剩余 ask 转为 deny。
9. 其余 ask/passthrough 进入用户确认。

显式 deny、受保护路径和工具声明的不可绕过检查不会被 bypass mode 静默覆盖。

## 规则

规则使用 `ToolName` 或 `ToolName(content)`。解析、规范化和 Bash rule 校验位于 `permissions.rules`；路径规则匹配位于 `permissions.path_rules`。

持久规则按来源保存为 allow、ask、deny 三组。共享项目设置不能开启 bypass mode；local 设置不应提交到版本控制。

## Host 交互

无头模式使用 `HeadlessPrompter` 并默认拒绝。TUI 通过 `chat.permissions.DeferredPermissionPrompter` 注册当前 handler；没有 handler 时同样 fail closed。

权限确认只决定是否执行当前调用。长期规则变更必须先成功持久化，再应用到活动策略，避免内存已放行但磁盘仍未记录。pending approval 是 host task 的短生命周期状态；完成、异常、拒绝、取消和 runtime close 都必须释放。

## 安全不变量

- 工具输入在权限决策前必须完成结构校验。
- 被拒绝或取消的调用也产生闭合 ToolResult。
- API key、原始工具输出和敏感路径不进入权限展示 DTO。
- PermissionPolicy 不读取设置文件，也不拥有配置存储。
- Chat、Agent 和 Tool 不保存第二份 runtime permission mode/rules。
