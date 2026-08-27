# MCP 与工具发现

> 当前已完成静态生命周期、增量 refresh、`tools/list_changed` 和全局 provider-neutral ToolSearch；resources/prompts 与 2026-07-28 driver 不在当前范围。

## 当前结构

`mcp` 是独立架构模块，拥有解析后 server spec、连接状态、transport/client 生命周期、远端 schema 校验、结构化诊断和标准 `Tool` adapter。`McpRuntime` 由 `AppState` 持有，第一次 turn 开始前惰性启动；Chat、Agent、Context 和 provider 均不感知 stdio 或 MCP wire 类型。

```text
settings -> bootstrap -> McpServerSpec
                         |
                         v
AppState.mcp -> McpRuntime -> McpTransport
                    |
                    +-> McpTool -> ToolCatalog source mcp:<server>
                                      |
                                      v
                         step ToolCatalogSnapshot -> ToolExecutor
```

每个 server 的全部工具以一个 `ToolSourceId("mcp", server)` 原子发布。发现、schema 校验或名称冲突失败时不会发布部分工具；断连和关闭会撤销 source。已经捕获旧 step snapshot 的 adapter 仍存在，但调用会得到规范化的 unavailable 结果，不会临时读取 catalog 最新版本。

## 配置与信任

MCP 默认关闭。settings 示例：

```json
{
  "version": 3,
  "mcp": {
    "enabled": true,
    "servers": {
      "search": {
        "command": "search-mcp",
        "args": ["--stdio"],
        "envFrom": {"SEARCH_TOKEN": "MY_SEARCH_TOKEN"},
        "enabled": true,
        "startupTimeoutSeconds": 10,
        "callTimeoutSeconds": 60
      }
    }
  }
}
```

`envFrom` 的 key 是传给 child process 的变量名，value 是当前进程中的来源变量名；settings 不接受包含字面量的 `env`。stdio child 只继承 PATH、HOME、locale、临时目录等基础变量以及显式引用，provider/API 凭据不会自动传入。

user、project、local 仍按既有优先级合并，同名 server 由高层完整替换。共享 project settings 可以声明 server，但默认禁用，且不能设置 `mcp.enabled=true` 或直接启用 server；需要把完整定义复制到不共享的 `settings.local.json` 后才允许启动。这是在尚无 workspace trust 数据库时的保守边界。

## 协议与生命周期

M5a 提供零新增依赖的 stdio 实现和可替换的窄 `McpTransport`。stdio 使用一行一个 UTF-8 JSON-RPC message，完成 `initialize` / `notifications/initialized`、分页 `tools/list`、`tools/call` 和 `notifications/cancelled`。当前驱动显式支持 2024-11-05 至 2025-11-25 的 legacy lifecycle；2026-07-28 的无会话 `server/discover` 协议需要新增独立 driver，不能伪装成兼容初始化。

连接状态为 `disabled`、`pending`、`connected`、`failed`、`closed`。启动失败只使对应 server 进入 failed，不阻止其他 server 或主 Agent；`reconnect()` 会先关闭旧 transport、撤销 source，再重新发现。诊断只含 server、状态、稳定 code 和清洗后的消息，不包含 command、args、stderr、env value 或远端异常正文。

关闭顺序为 TaskSupervisor → AgentRunFactory → SkillRuntime → McpRuntime → ProviderRuntime。stdio 先关闭 stdin，随后按 wait → terminate → kill 回收进程，并取消/等待 stdout、stderr reader task。

## 工具安全边界

- MCP tool name 映射为 `mcp__<server>__<remote>`；`.` 使用 `_dot_` 编码，`-`/`_` 保留。超过 64 字符时追加 identity hash 做稳定截断，原始 server/tool identity 仍保留在 adapter 和 metadata 中；任何最终冲突都会使整组发现失败。
- 根 input schema 必须声明 `type: object`。M5a 的无依赖 validator 支持 object/array/基础类型、required、additionalProperties、enum/const、组合与常见长度/数值约束；`$ref` 明确拒绝。
- MCP adapter 默认不声明只读或并发安全，也不信任 server 自报字段放宽本地策略。
- adapter 返回 passthrough 权限结果，因此默认模式需要确认；显式 allow/ask/deny、dontAsk 和 bypass 仍由统一 `PermissionPolicy` 决定。
- 参数校验发生在权限询问和远端 I/O 之前；执行、审计、hook、取消和 ToolResult 闭合仍只经过 `ToolExecutor`。
- tool result 的 text 与 structured JSON 被规范化为当前 provider-neutral 文本结果。MCP resources/prompts 和富媒体产品体验不在 M5a 范围。

## 增量发现与全局 ToolSearch

`McpRuntime.refresh(server)` 重新拉取完整 tool list，先完成全部 schema/name/catalog 冲突校验，再以一个 source replace 发布。内容相同的 refresh 不增加 catalog version；非法更新保留旧 source 并只更新结构化诊断。stdio 的 `notifications/tools/list_changed` 会触发同一路径；同一 server 的并发通知被合并，refresh 期间到达的新通知会保证再执行一次 diff，不会静默丢失。

每个 MCP server 无论工具数量多少都把全部 adapter 发布到完整 ToolCatalog，并统一声明为 searchable。全局 `ToolSearch` 搜索所有 searchable 工具的 name/description，也支持 `select:ToolA,ToolB` 精确选择；当前可搜索名称由临时 attachment 公布，不披露 schema。

命中 definition 与规范化 JSON 的 SHA-256 fingerprint 作为 durable discovery attachment 写入 Session，从下一 model step 生效。dispatcher 模式通过 `InvokeSearchedTool` 路由；native 模式才把有效命中加入顶层 definitions。refresh 后未变化的 fingerprint 继续有效；description、schema、名称变化或删除会产生 durable invalidation，要求重新搜索。compact 会把仍有效的 discovery state 重挂到 summary 后，恢复不依赖进程内集合。
