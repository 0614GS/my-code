# my-code

**一个模块化、类型安全、上下文透明的 Python Coding Agent。**

my-code 借鉴 Claude Code 的 Agent loop、工具、权限与上下文管理思路，但不以复刻产品为目标。它更关注一件事：用一套规模可控、边界清晰、能够被测试验证的代码，构建真正可用的终端 Coding Agent。

> A modular, provider-neutral and glass-box coding agent — inspect the architecture, not just the output.

## 技术特点

- **Provider-neutral**：模型协议被隔离在边界层，Agent 核心不绑定某一家模型服务，可以在不改变核心工作流的前提下适配不同 Provider。
- **类型驱动**：用明确的领域模型表达请求、会话、工具与权限，让组件之间的契约可检查、可理解，也更容易安全演进。
- **模块化且有边界**：每个模块拥有清晰职责，依赖保持单向；架构守卫把这些原则变成持续生效的自动化约束。
- **状态透明**：会话是事实的唯一来源，上下文只是面向模型的投影。状态从哪里产生、如何变化、何时持久化都有明确归属。
- **统一扩展模型**：内置工具、MCP、Skills、Subagent 和后台任务复用同一套执行与权限机制，扩展能力无需侵入 Agent 核心。
- **失败也是设计的一部分**：拒绝、取消、中断、溢出与恢复都被当作正常生命周期建模，而不是只优化理想路径。

这些原则共同指向一个目标：让 Coding Agent 不只是“能够运行”，也能够被阅读、验证、修改和长期维护。具体架构见 [架构手册](docs/README.md)。

## 与 Claude Code 的关系

Claude Code 是 my-code 最重要的行为参考之一：两者都围绕终端 Agent、权限确认、会话恢复、上下文压缩、MCP、Skills 和 Subagent 展开。my-code 选择了不同的工程重点：

| | Claude Code | my-code |
| --- | --- | --- |
| 核心定位 | 成熟的 Claude 编程产品 | 小型、可研究、可修改的 Agent 实现 |
| 模型边界 | 围绕 Claude 及其托管渠道 | Anthropic / OpenAI 协议之上的 provider-neutral core |
| 架构可见性 | 以产品行为和公开文档为主要接口 | 源码、状态所有权和架构不变量都在仓库中 |
| 模块约束 | 不推测其内部实现 | Pyright + AST dependency/API guards 自动验证 |
| 终端界面 | Claude Code 自有交互体验 | `prompt_toolkit + Rich`，保留原生 terminal scrollback |
| 项目阶段 | 完整产品与生态 | 早期开发，优先保持实现清晰和可验证 |

my-code 不追求逐项追平 Claude Code，也不会机械翻译其实现。参考行为、安全不变量和有意保留的差异记录在 [MVP scope](docs/08-mvp-scope.md) 中。

## 已实现能力

- 多 step Agent loop 与流式 text、reasoning、usage
- Read、Write、Edit、Glob、Grep、Bash 与 Todo 工具
- 安全并行 ToolRound、权限规则和工作区边界
- 持久化 Session、恢复、Token 预算与自动/手动 Compact
- 前台/后台 Subagent、独立 child Session 与任务取消
- MCP stdio 工具发现、ToolSearch 与 Skill 按需激活
- Anthropic Messages、OpenAI Responses 和兼容协议网关
- 非全屏 TUI、原生 scrollback、Slash commands 与 `@path` attachment

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和一个受支持的 Provider。

```bash
git clone https://github.com/0614GS/my-code.git
cd my-code
uv tool install .
mycode
```

首次启动会先打开 Provider 向导；完成或选择一个 profile 后才会创建聊天运行时。向导支持 Anthropic Messages 与 OpenAI Responses，使用模型目录接口检查连接并把安全模型目录保存到 profile，不发送推理请求。进入聊天后可用 `/provider` 再次打开同一配置流程，或用无参数 `/model` 从当前 Provider 的本地目录即时切换模型。

Provider 的协议、Base URL、默认模型和模型目录只来自 `~/.my-code/providers.json`，API Key 只保存在私有的 `~/.my-code/.credentials.json`，当前 Provider 选择只保存在 `settings.json.activeProvider`。Provider 环境变量以及旧的 `--model`、`--base-url` 和 `agent.model` 均不再支持；v2/v3 profile 会在下一次 Provider/模型写入时升级为 v4。

键入 `/` 查看可用命令；使用 `@path` 将文件或目录加入对话。

## 项目状态与文档

my-code 仍处于早期开发阶段。它已经具备完整的交互式 Agent 主路径，但目前提供的是应用层权限与工作区保护，不是 OS 级沙箱；请勿对不受信任的项目关闭权限确认。

- [架构手册](docs/README.md)
- [当前 MVP 范围](docs/08-mvp-scope.md)
- [存储与配置](docs/09-storage-and-settings.md)
- [模块边界与依赖方向](docs/12-package-boundaries.md)

## 参与开发

```bash
uv sync --group dev
uv run ruff check .
uv run pyright
uv run pytest
```

欢迎提交 Issue 和 Pull Request。
