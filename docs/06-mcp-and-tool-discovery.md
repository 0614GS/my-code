# MCP 与工具发现

## 1. MCP 是动态工具来源

MCP 没有独立的执行循环。连接成功后，远端 tool 被转换成普通 `Tool`，进入同一个 Registry、权限管线、调度器和结果预算。

```text
MCP config
  → connection state
  → tools/list
  → MCP schema 转 Tool
  → assembleToolPool
  → 普通 tool execution pipeline
```

这使内置工具和远端工具只在“如何发现、如何调用”上不同，Agent Loop 不需要分叉。

## 2. 配置与连接状态

MCP 支持 stdio、SSE、HTTP、WebSocket、SDK 等 transport。连接是判别联合类型：

```text
pending | connected | failed | needs-auth | disabled
```

`connected` 状态持有 client、capabilities、server info、instructions、config 和 cleanup；其他状态仍保留 server name/config，便于 UI、重试和 ToolSearch 报告。

连接层的关键策略：

- 本地进程与远端网络连接使用不同批量并发度。
- 连接按 server config 缓存，config 改变或 transport 关闭时清缓存。
- HTTP/SSE auth 失败可进入 `needs-auth`，而不是混成普通工具错误。
- transport 异常会清除连接及 tools/resources cache，使下次操作重新连接。
- 连接与单次 tool call 都有独立 timeout 和取消信号。

类型和实现位于：

- `claude-code/src/services/mcp/types.ts`
- `claude-code/src/services/mcp/client.ts`

## 3. 远端 Tool 转换

`fetchToolsForClient()` 调用 `tools/list`，清理远端数据后为每个定义创建本地 Tool：

- 默认名为 `mcp__<server>__<tool>`；
- 保存未规范化的 `mcpInfo.serverName/toolName`；
- MCP `inputSchema` 直接作为 JSON Schema；
- `readOnlyHint` 决定 read-only 和 concurrency-safe；
- `destructiveHint`、`openWorldHint` 映射为风险属性；
- `_meta['anthropic/alwaysLoad']` 控制是否跳过延迟加载；
- 远端 description 有长度上限；search hint 会压平换行。

MCP Tool 默认返回 `passthrough` permission，通常需要用户确认或显式 allow rule。权限匹配始终使用 fully-qualified name，即使 SDK 模式允许远端工具以无前缀名称覆盖内置显示名。

## 4. 工具调用

MCP Tool 的 `call()` 会：

1. 发送 started progress。
2. 通过 `ensureConnectedClient()` 获取仍有效的连接。
3. 发起 MCP `tools/call`，传递取消信号和 tool-use metadata。
4. 处理 server progress 与 URL elicitation。
5. 转换文本、图片、blob、structured content 和 `_meta`。
6. 发送 completed 或 failed progress。

如果 server session 过期，连接缓存会被清除，调用以新连接重试一次。普通业务错误不会无限重连。

远端结果最终仍通过 `mapToolResultToToolResultBlockParam()` 生成标准 tool result，并受统一大小限制和 hooks 处理。

## 5. 为什么延迟加载工具

MCP server 可能暴露大量工具。把所有 description 和 JSON Schema 固定放进每次请求会：

- 占用大量上下文；
- 增加首轮延迟和费用；
- 使 server 工具变化频繁破坏 prompt cache。

因此 Tool 可以标记为 deferred：初始只告诉模型工具名，需要时再取完整 schema。

`isDeferredTool()` 的主要规则：

- `alwaysLoad` 优先，命中时不延迟；
- MCP Tool 默认延迟；
- 普通 Tool 通过 `shouldDefer` 选择延迟；
- ToolSearch 自身永远不能延迟。

是否实际启用还取决于运行模式、模型是否支持 `tool_reference`、网关能力和工具 schema 总量。

## 6. ToolSearch 协议

`ToolSearchTool` 支持两种查询：

```text
select:ToolA,ToolB    # 按精确名称加载
github create issue  # 按名称、description 和 hint 检索
```

返回值中的每个匹配项被编码为 `tool_reference`。服务端展开为完整 tool definition，此后模型才能按 typed schema 调用该工具。

搜索没有结果时会返回仍处于 pending 的 MCP server，提示模型稍后重试。直接选择一个已经加载的工具被视为无害的成功，避免模型陷入重复搜索。

实现位于：

- `claude-code/src/utils/toolSearch.ts`
- `claude-code/src/tools/ToolSearchTool/`

## 7. 跨轮与 Compact

已发现工具集合从消息历史中的 `tool_reference` 推导，而不是只保存在临时内存 Set。这样下一轮可以继续发送已加载 schema。

完整 compact 会丢掉旧 tool-reference block，因此 boundary metadata 会保存 compact 前的 discovered tool names；compact 后还会重新注入 deferred-tool delta。MCP 中途连接完成时，query loop 在下一迭代刷新工具池。

## 8. 安全边界

MCP server 提供的名称、description、schema 和输出均是不可信输入：

- 名称需要规范化和命名空间隔离；
- description/search hint 需要限长、去除注入式换行；
- blanket deny 在工具暴露给模型前过滤；
- 执行仍经过 Schema、Permission、Hook 和结果预算；
- transport auth 与工具授权是两套独立状态；
- 连接重试必须有界。

## 9. 核心不变量

1. MCP 只扩展 Tool 来源，不绕过统一执行和权限管线。
2. 权限规则使用稳定的 fully-qualified identity。
3. 连接状态、auth 状态和单次调用结果不能混为一个异常类型。
4. deferred tool 在 schema 被发现前不可按猜测参数执行。
5. 工具集合变化必须保持稳定排序，并可在迭代边界刷新。

## 10. 主要源码入口

- `claude-code/src/services/mcp/{types,client,utils}.ts`
- `claude-code/src/tools/MCPTool/`
- `claude-code/src/tools/ToolSearchTool/`
- `claude-code/src/utils/toolSearch.ts`
- `claude-code/src/tools.ts`
