# 工具系统

## 1. Tool 是协议对象

`Tool` 不只是可调用函数。参考实现的接口同时描述五类信息，定义在 `claude-code/src/Tool.ts:362`：

| 类别 | 代表字段 |
| --- | --- |
| 身份与发现 | `name`、`aliases`、`searchHint`、`isMcp`、`shouldDefer` |
| Schema | `inputSchema`、`inputJSONSchema`、`outputSchema` |
| 调度属性 | `isEnabled`、`isConcurrencySafe`、`interruptBehavior` |
| 风险属性 | `isReadOnly`、`isDestructive`、`isOpenWorld`、`checkPermissions` |
| 执行与结果 | `validateInput`、`call`、`mapToolResultToToolResultBlockParam` |

接口还包含 `getToolUseSummary`、`getActivityDescription`、
`renderToolUseMessage` 和 `renderToolResultMessage` 等展示方法。关键思想不是让顶层
执行器识别每种工具，而是由最了解领域输出的 Tool 提供展示语义；React/Ink 组件
仍属于 Claude Code 前端实现，不能直接泄漏进 Python 核心层。

## 2. ToolResult

`call()` 返回的 `ToolResult<T>` 可包含：

- `data`：工具的领域输出；
- `newMessages`：工具额外产生的内部消息；
- `contextModifier`：成功后对 `ToolUseContext` 的变换；
- `mcpMeta`：MCP 的 `_meta` 和 `structuredContent`。

领域输出不会直接塞进对话。`mapToolResultToToolResultBlockParam()` 负责生成带正确 `tool_use_id` 的 API `tool_result`，之后再经过大小预算和持久化处理。

## 3. 默认实现

`buildTool()` 为常用字段补默认值：

- enabled：`true`
- concurrency-safe：`false`
- read-only：`false`
- destructive：`false`
- permission：返回 allow 和原输入
- classifier input：空字符串
- user-facing name：工具名

这些是当前源码的兼容性默认，不应自动视为理想安全策略。尤其 permission/destructive 元数据需要每个有副作用的 Python 工具显式声明。

## 4. 工具池组装

工具集合不是全局常量，而是按当前环境和权限上下文投影：

1. `getAllBaseTools()` 枚举构建产物中可能存在的内置工具。
2. `getTools()` 应用 feature、simple mode、REPL mode、`isEnabled()` 等过滤。
3. blanket deny 的工具在发送给模型前直接移出集合。
4. `assembleToolPool()` 合并内置工具和 MCP 工具。
5. 同名冲突时内置工具优先。
6. 内置与 MCP 分区后分别按名称排序，保持 prompt cache 前缀稳定。

工具名查找支持 alias；只在恢复旧 Transcript 等兼容场景下回退到已废弃名称。

主要实现位于 `claude-code/src/tools.ts` 和 `claude-code/src/utils/toolPool.ts`。

## 5. 单次执行管线

`runToolUse()` 与 `checkPermissionsAndCallTool()` 形成统一执行管线：

```text
按 name/alias 查找工具
  → Schema 类型校验
  → validateInput 语义校验
  → 防御性清理内部字段
  → 在克隆输入上补充观察字段
  → PreToolUse hooks
  → 权限决策
  → Tool.call
  → 映射 tool_result
  → 大结果外置
  → PostToolUse / PostToolUseFailure hooks
  → 生成内部 Message
```

Schema 错误、语义错误、未知工具、权限拒绝和执行异常都会转换成 `is_error: true` 的 tool result，而不是仅抛异常终止循环。这样模型能够看到失败并调整下一步。

`backfillObservableInput()` 只修改克隆：SDK、Transcript、hook 和权限层可以看到派生字段，但重新发送给模型的原始 tool input 不被改写，从而保护 prompt cache 和 thinking 签名。

对应实现：`claude-code/src/services/tools/toolExecution.ts`。

## 6. 批量调度

非流式调度器 `runTools()` 把调用划分成两类批次：

- 连续的 concurrency-safe 调用组成并行批次；
- 每个非 concurrency-safe 调用单独串行执行。

并行资格在 Schema 解析成功后由工具根据具体输入判断；判断抛错时按不安全处理。非流式兼容路径会按 tool ID 暂存 modifier，再按原始工具顺序应用；但工具协议只保证非 safe 工具的 context modifier，调用方不应依赖并行 modifier。

实现位于 `claude-code/src/services/tools/toolOrchestration.ts`。

## 7.1 nano-code 的 ToolInteractionPort

nano-code 将一次 assistant 工具轮次收敛到 `ToolInteractionPort`。它只暴露串行
`run_round()`、调用展示和历史结果展示；`ToolRoundExecutor` 是现有 `ToolExecutor`
的适配器，负责按顺序执行、生成开始/结束事件、绑定 session-scoped 结果目录，并在
取消时为尚未完成的每个 `tool_use` 补上错误 `tool_result`。AgentEngine 只消费这些
事件并决定是否进入下一模型轮，不识别具体工具或权限实现。

本轮仍保持 MVP 的串行策略。未来并行调度、并发安全工具和取消屏障应只修改
`ToolRoundExecutor`，不扩张 AgentEngine 的职责。

## 7. 流式调度

`StreamingToolExecutor` 在模型还在输出时接收完整 tool block。每个调用经历：

```text
queued → executing → completed → yielded
```

调度规则：

- 当前无运行工具时，队首工具可启动。
- 当前全是 concurrency-safe 工具时，新的 safe 工具可并行启动。
- 非 safe 工具必须等待所有运行工具结束，并阻止后续越过它。
- progress 不受最终结果顺序约束，立即发出。
- 已完成的 safe 工具结果可以先发出；非 safe 工具是结果与执行顺序的屏障，结果关联依靠 tool ID 而非完成顺序。

Bash 工具报错会取消并行 sibling，因为批量 shell 命令常存在隐式依赖；Read/WebFetch 等独立读失败不会取消其他读取。

取消使用父子 `AbortController`：用户取消可向下传播，sibling error 只取消本批工具而不直接终止整个 query controller。

实现位于 `claude-code/src/services/tools/StreamingToolExecutor.ts`。

## 8. 结果大小与上下文变更

每个工具声明单结果阈值；执行器统一把超大文本落盘并返回预览。聚合结果预算和跨轮稳定替换由上下文层负责，见 [03-context-management.md](03-context-management.md)。

`contextModifier` 只适用于不会并发冲突的变更。流式执行器不支持 safe 并行工具修改 context；需要共享状态的工具应串行，或把状态更新改造成可交换事件。

## 9. 核心不变量

1. 工具输入在 Schema 和语义校验通过前不得进入权限或执行层。
2. 调度属性由具体输入决定，异常时保守地串行。
3. 工具异常必须产生协议完整的 tool result。
4. 并行完成顺序不可作为业务语义；关联依靠 tool ID，共享 context 变更必须串行。
5. 模型原始 tool input 与观察层派生 input 必须分开。
6. 工具池排序和冲突规则必须稳定，否则会破坏 prompt cache。

## 10. nano-code 的职责边界

### 权限语义

nano-code 保留静态 `risk` 作为默认分类，同时让工具按具体输入实现
`is_read_only(input, context)` 和 `check_permissions(input, context)`。后者返回
`allow | ask | deny | passthrough`，只表达工具领域内的事实；全局规则、mode 和
最终 ask 收敛仍由 `PermissionPolicy` 统一处理。`ToolExecutor` 不识别 Bash、Read
等具体类型，只保证 validation → permission → execution → result 的顺序。

Bash 的命令解析和参数白名单因此位于 `tools/builtin/bash_permissions.py`，而不是
Executor。权限层批准了修改后的 input 时，Executor 必须把同一个 input 交给弹窗
和执行阶段，避免“用户批准 A、工具执行 B”。

### 双重结果投影

一次 Tool 执行产生两种用途不同的结果：

```text
ToolOutput（领域输出 + metadata）
  ├─ Tool.to_model_result()  → ToolResultBlock → Provider / Transcript
  └─ Tool.present_result()   → ToolResultPresentation → Runtime event → TUI
```

`Tool` 同时通过 `present_use()` 提供显示名、调用摘要和活动描述。内置 Tool 从自己
产生的结构化 `metadata` 中构造结果摘要；TUI 不按工具名分支，也不解析模型可见的
结果字符串。`ToolExecutor` 只负责调用两种投影、结果外置和错误回退，因此新增工具
无需修改 Executor 或 TUI。

展示对象只包含文本、布尔值等前端无关数据，不依赖 Textual。实时事件和权限弹窗
消费同一份调用展示对象；成功结果的展示快照随 `ToolResultBlock` 写入 Transcript，
保证 `/resume` 不受后来展示算法变化影响。旧记录没有快照时，runtime 把旧结果交回
对应 Tool 做兼容投影。未知工具或展示方法异常时，Executor 使用有界通用摘要。

这一设计保留了 Claude Code 的“Tool 拥有展示语义”，同时维持
Tool → Agent → Runtime → TUI 的单向依赖和核心 runtime 与具体前端解耦。

## 11. 主要源码入口

- `claude-code/src/Tool.ts`
- `claude-code/src/tools.ts`
- `claude-code/src/services/tools/{toolExecution,toolOrchestration,StreamingToolExecutor}.ts`
- `claude-code/src/utils/toolPool.ts`
- `claude-code/src/utils/toolResultStorage.ts`
