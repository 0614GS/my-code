-- Active: 1782987758255@@115.190.199.119@3306@resume_match
# 当前能力范围

本文只记录当前实现、明确延后的能力和有意保留的产品差异。具体机制由相应专题解释。

## 已实现

- Anthropic Messages、OpenAI Responses 和兼容网关上的 provider-neutral 模型边界。
- 流式 text、reasoning、usage、continuation 和模型目录/能力建模。
- 多 step Agent loop、并行安全 ToolRound、step/token 限制和取消闭合。
- Read、Write、Edit、Glob、Grep、Bash、TodoWrite、ToolSearch 与动态 invoker。
- allow/ask/deny、交互确认、dontAsk、Full Access 和工作区路径复验。
- canonical Conversation、JSONL Session、session permission mode、resume、自动/手动/reactive compact。
- Codex 风格 Default/Plan collaboration mode、追加式 mode prelude、Question 交互和 proposed-plan 实施交接；v1 不提供 `/plan`、多选问题或 headless Question 回答。
- `@path` 文件/目录 Attachment、AGENTS 用户上下文和路径补全。
- foreground/background Subagent、独立 child run/Session/provider lease 和权限收窄。
- Bash/Subagent 统一后台任务、查询、取消和单次完成通知。
- MCP stdio 工具发现、refresh/reconnect、全量 catalog 和标准权限执行。
- Skill 分层发现、严格 frontmatter、lazy load、reload、durable activation 和 additive session rules。
- Provider profile、私有凭据、模型发现目录和运行期 `/provider`、`/model` 切换。
- 基于 `prompt_toolkit + Rich` 的非全屏 TUI、原生 scrollback 和只读 transcript/agent pager。
- Concise/Detailed 主界面密度、用户级 `/view` 偏好，以及带 resolved request 导航的 Audit transcript。
- Session 私有、内容寻址的 provider-neutral request audit sidecar，覆盖 Agent、continuation、compact/retry 与 child run。
- Agent active 时可编辑 composer、session-bound 临时输入队列、FIFO step-boundary steering 与 Up 召回。
- 单 UI owner、单 worker 离屏 scrollback render，以及有界/latest-wins live Markdown projection。
- Codex 风格的结构化命令信息卡，以及由显式 model-step 边界驱动的工作组/最终回答分隔。
- 带 spinner 和 invocation 计时的统一活动行，以及 manual/auto/reactive full compact 生命周期提示。
- token budget 统一驱动的自动压缩、首条用户输入后的 context 状态展示，以及 dispatcher-aware Todo 实时/恢复投影。
- Ruff、Pyright standard、pytest 和 AST 架构守卫。
- Linux Bash 的 Bubblewrap 挂载与 namespace 隔离、启动探测/fallback 状态，以及受保护 workspace 元数据占位租约。
- 真实 sandbox 内 Bash 默认执行与逐命令 `require_escalated` 宿主授权；跨 Agent 权限提示串行且越界批准不可记忆。

Subagent 和后台任务默认开启；MCP 与 Skill 默认关闭。Headless 入口不启用后台执行。

## 明确延后

- MCP resources/prompts、富媒体结果和更新版 driver。
- 通用 Hooks runtime 与配置 schema。
- Plan Mode 产品能力。
- Session fork、远程会话和跨设备同步。
- 图片、音频等媒体 Attachment。
- OAuth、系统 Keychain 和团队凭据管理。
- macOS/Windows sandbox、完整应用容器、seccomp/Landlock 与域名级网络代理。
- Provider 断线续传和 stream 重放。
- 后台任务跨进程恢复或远端执行。

## 长期不变量

- Provider SDK 类型不离开 `providers`。
- Session 是 canonical conversation 及其持久化的唯一公开权威来源。
- request audit 是 Session 持久化审计事实，但不属于 canonical Conversation，也不参与 context planning。
- Context 是单次请求投影，不保存第二份可写历史。
- ToolCall 在执行前已经作为完整 AssistantMessage 提交，拒绝与取消仍产生闭合结果。
- 活动 Session 与 `ContextRuntime` 原子配对，cache 不跨 resume 或 child run 混用。
- 动态能力只在 step 边界生效，并统一经过 Tool、权限和 Session 路径。
- Subagent 只能收窄权限，child transcript 不合并进 parent Session。

## 与参考实现的有意差异

`claude-code/` 只用于学习行为和安全不变量，不是本项目的包结构来源。my-code 保留以下选择：

- 核心模型同时支持 Anthropic/OpenAI 协议，以 provider-neutral items 表达输入和工具结果。
- TUI 使用 Python 原生非全屏 host 与 terminal scrollback，不复刻 Ink/Textual renderer。
- 第三级 transcript 展示 my-code 语义请求而非 Provider SDK/wire payload；历史缺失时明确留空，不用当前配置回填。
- pending queue 属于 host/runtime 临时状态，不迁移到 canonical Session；进程退出时未接受输入允许丢失。
- Provider、凭据和模型目录都有显式持久化来源，不从模型名或环境变量猜测能力与连接。
- Skill Markdown 是数据，不导入目录代码；激活正文使用 Attachment，而不是修改 system prompt。
- Subagent 使用固定角色、fresh child Session 和独立 lease；默认不设置 step/token/timeout 上限，但保留显式可选限制。
- ToolSearch 使用 provider-neutral dispatcher/native 模式，不发送特定 Provider 的 defer/reference wire 类型；canonical dispatcher call 与 feature-facing 目标语义分别保留。
- 后台完成用无 payload wake signal 触发 pull，再通过 durable attachment 交付，不建立第二份消息队列。
- Linux Bubblewrap 只隔离 Bash 进程树；MCP、Provider 与 my-code 主进程仍在宿主运行，应用层权限继续作为独立前置边界。

调整上述差异或长期不变量时，应同时修改对应架构专题、测试和本文件。
