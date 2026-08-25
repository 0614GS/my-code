# 模块划分

## 目标目录

以下是目标边界，不要求一次性创建所有文件。文件应随实际职责增减。

```text
src/my_code/
├── model/              # provider-neutral 模型调用
├── conversation/       # canonical 对话事实和内存聚合
├── context/            # Conversation + 临时上下文 -> ModelRequest
├── tools/              # 工具定义、注册和受控执行
├── permissions/        # 授权规则与决策
├── workspace/          # 工作区路径和本地文件能力
├── sessions/           # session 生命周期与 JSONL 持久化
├── agent/              # 一次 turn 的模型/工具循环
├── chat/               # 用户级聊天编排
├── features/           # 独立的纵向产品能力
├── providers/          # ModelClient 的具体实现与配置
├── prompts/            # system prompt 片段和解析
├── config/             # settings、路径和分层配置
├── auth/               # provider 凭据
├── tui/                # Textual host
├── cli/                # 命令行 host
└── bootstrap.py        # 唯一组合根
```

## 核心模块

### `model`

拥有统一的模型语言：`ModelClient`、请求、响应、流式事件、内容块、resolved system prompt、usage、capabilities、JSON 值和 provider continuation envelope。Conversation、Tool、Permission 和 UI 使用的相关值对象也从这里引用，不再各自定义或经 Agent 转发。

`ModelClient` 是必要的 Protocol，因为已有多个 provider 实现。它只声明 `stream()`；需要完整响应时使用 `collect_model_output()` 收集唯一的 `ModelOutputCompleted`，不再建立第二个 completion port。

### `conversation`

拥有已发生的对话事实及内存中的 `Conversation` 聚合：消息、内容块、ToolCall、ToolResult、父链、派生 context entries、compact boundary 和 content replacement。

它不读写 JSONL，不调用模型，不执行工具，也不包含 UI presentation 或临时 attachment delivery。

### `context`

把 Conversation 和临时 attachment 投影为 ModelRequest，并负责预算、规范化、microcompact 和 compact。

无状态的 `ContextEngine` 对外提供 plan、inspect 和 compact；内部 `ContextPlanner` 负责确定性投影，`ContextCompactor` 负责摘要模型调用。每个活动 session 的 `ContextSession` 只保存不落盘的 live-session attachment delivery 和 session 级缓存。Context 不复制 Conversation history，不执行工具，不读取 TUI 状态，不解析 provider wire payload。

### `tools`

拥有 Tool 基类、定义、输出、registry、invocation、executor 和通用 presentation。内置的文件与 Bash 工具位于 `tools/builtin`。

ToolCall 和已提交的 ToolResult 是对话事实，归 `conversation`；尚未提交的执行结果归 `tools`。

### `permissions`

拥有 PermissionRequest、规则、模式、decision、用户 approval 数据和更新语义。Policy 接收结构化 PermissionRequest，不接收整个 Tool 实例。

### `workspace`

拥有 canonical path、工作区与符号链接边界、相对展示路径以及具体本地文件能力。它不解释 PermissionRule，也不决定授权。

### `sessions`

拥有 `Session`、JSONL record、codec、store 和 catalog。当前只有 JSONL 实现，因此不建立 Repository Protocol。

`Session` 持有一个 `Conversation`，并为它增加身份、持久化优先提交和恢复生命周期。所有可恢复事实只能经 Session 提交；Conversation 本身不做 I/O。

需要恢复的 Tool presentation 作为 Session 的附加 record 和索引引用 `tools` 类型，但不得因此写回 Conversation canonical facts。

### `agent`

拥有一次 turn 内的模型调用、工具轮、transition、终止和事件。`AgentEngine` 直接使用 ModelClient、ContextEngine、ToolRoundExecutor 和调用方传入的 Session，不再通过 inbound/outbound port 转发，也不代理 inspect、manual compact 或历史 presentation。

Agent 不在 turn 之间保存活动 Session，也不负责 resume。它不拥有 UI 状态、provider wire DTO、JSONL schema 或 feature-specific 状态。

### `chat`

拥有用户级编排：提交与取消、活动 Session 和 ContextSession 的原子切换、provider 选择、权限确认、attachment 加载，以及 AgentEvent 到 ChatEvent 的投影。

当前只有一个交互 runtime，因此直接提供 `ChatService`，不建立 `ChatRuntime` Protocol。

详细的消息来源、状态生命周期与恢复事务见 [03-state-lifecycle.md](03-state-lifecycle.md)。

## 支撑模块

- `providers`：OpenAI、Anthropic 适配及运行时 catalog/manager/router；实现 `model` 定义的能力，并读取 `config` 的 profile 配置。
- `prompts`：未解析 prompt section、生命周期和解析；解析结果使用 `model.SystemPrompt`，不拼接 provider payload。
- `config`：路径、分层 settings、provider profile schema/store 和权限更新持久化协调；不负责运行时组装。
- `auth`：凭据读取、保存和遮蔽。
- `tui`、`cli`：host，只调用 `chat` 的公开能力。
- `bootstrap.py`：创建具体对象并连接模块，不包含业务判断。

## Features

每个 feature 是独立架构节点，而不是共享内部状态的大包。

- `features.file_mentions`：mention 语法、加载和路径建议。
- `features.todos`：Todo 模型、Conversation 投影、reminder 和 Tool 实现。
- `features.subagents`：子 Agent 创建、约束、运行与取消；实现时再建立。
- `features.plan_mode`：计划模式状态、prompt 和工具可用性策略；实现时再建立。

Feature 可以组合基础模块，但基础模块不得为了某个 feature 导入它。注册由 `bootstrap.py` 完成。
