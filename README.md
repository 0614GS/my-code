# my-code

一个模块化、上下文透明的 Python coding agent。它借鉴 Claude Code 的上下文、工具和权限边界，但以小型、可测试的实现表达，并计划把真正发送给模型的请求投影到 TUI 中。

> A modular, context-transparent Claude Code alternative—see exactly what the model sees.

Python 包和 CLI 分别使用 `my_code` 与 `mycode`；环境变量使用 `MY_CODE_*` 前缀，用户及项目配置存放在 `.my-code` 目录。

## 安装并启动

使用 uv 安装命令行工具后，可以直接用 `mycode` 启动：

```bash
uv tool install --editable .
mycode --help
mycode
```

`--editable` 适合本地开发；如果只想安装当前版本，可去掉该选项。卸载命令为
`uv tool uninstall my-code`。

## 开发

```bash
uv sync --group dev
uv run mycode --help
uv run mycode
```

首次使用在 TUI 中打开 `/provider`，可配置 Provider、模型、URL 和 API Key。Key 按 Provider 保存在 `~/.my-code/.credentials.json`，文件权限为 `0600`，不会进入项目配置或 Transcript；Provider 面板会显示当前凭据来自环境变量、本地存储或尚未配置，并可二次确认后删除本地 Key。环境变量 Key 不受删除操作影响。

`~/.my-code/providers.json`：

```json
{
  "version": 1,
  "providers": {
    "company-gateway": {
      "protocol": "anthropic-messages",
      "model": "compatible-model-name",
      "baseUrl": "https://gateway.example.com/anthropic"
    }
  }
}
```

```bash
mycode --provider company-gateway
mycode --session 11111111-1111-1111-1111-111111111111
```

`MY_CODE_PROVIDER`、`MY_CODE_API_KEY`、`ANTHROPIC_BASE_URL`、`--provider` 和 `--base-url` 可用于临时覆盖。

`mycode` 只启动交互式 `prompt_toolkit + Rich` 非全屏 TUI：完成的 Markdown、工具结果和回合统计保留在普通终端 scrollback，底部只动态维护多行输入、补全、活动状态与临时面板。需要授权时，输入区会切换为允许、拒绝、反馈和可选 remember；拒绝原因会返回模型。在输入框键入 `/` 会显示透明候选菜单并默认选中第一项，Enter 直接执行、Tab 只补全；TUI 保留终端原生光标形态与闪烁。`/provider` 可安全配置 Provider URL、模型和 API Key，`/resume` 可选择并恢复历史会话，`/usage`、`/tools`、`/skills`、`/mcp`、`/tasks` 展示已有运行时能力。`--session <uuid>` 会在 TUI 中直接恢复指定会话。

聊天启动要求 stdin 和 stdout 都连接 TTY；管道输入和重定向输出会在创建配置、发现 Provider 或访问网络前失败。`--help` 与 `--version` 不受此限制。未来配置类子命令可以单独支持非 TTY，但当前没有 MCP/Skill 配置子命令。

与 Claude Code 的目录边界一致，用户配置及运行状态默认放在 `~/.my-code/`，可用 `MY_CODE_CONFIG_DIR` 整体迁移。项目共享配置为 `.my-code/settings.json`，本地覆盖为已忽略的 `.my-code/settings.local.json`；读取配置不会主动创建这些目录。完整布局和优先级见 [docs/09-storage-and-settings.md](docs/09-storage-and-settings.md)，终端层边界见 [docs/10-terminal-ui.md](docs/10-terminal-ui.md)。

实验性的 MCP 已可通过 settings 注册 stdio server，默认关闭，并支持增量发现与大工具集的 deferred ToolSearch。共享项目只能声明、不能直接启用 MCP 命令；需复制到 `settings.local.json` 显式信任。远端工具仍经过本地 schema、权限和取消流水线，详细配置见 [docs/06-mcp-and-tool-discovery.md](docs/06-mcp-and-tool-discovery.md)。

Skill 加载同样默认关闭。启用 `skills.enabled` 后，my-code 按项目 `.my-code/skills`、用户配置目录 `skills`、内置来源的顺序发现 `<name>/SKILL.md`；模型通过标准 `Skill` Tool 选择后，完整 Markdown 只在下一 step 出现一次，可选 `allowed-tools` 只能收窄该 step 的工具集。Loader 不导入 Skill 目录里的 Python 或执行 shell，格式和验收边界见 [docs/13-extensibility-roadmap.md](docs/13-extensibility-roadmap.md#m6skill-加载与按需激活)。

首次启动 TUI 时会创建缺失的用户级 `settings.json`、`providers.json`、空 `.credentials.json` 和 `projects/`。Provider Profile 将 URL、协议和默认模型与凭据分离。

```bash
uv run ruff format .
uv run ruff check .
uv run pyright
uv run pytest
```

架构阅读笔记从 [docs/README.md](docs/README.md) 开始；当前实现边界见 [docs/08-mvp-scope.md](docs/08-mvp-scope.md)，提示词职责见 [docs/11-prompt-management.md](docs/11-prompt-management.md)。`claude-code/` 仅是本地只读参考源码，已被 `.gitignore` 排除，禁止提交或发布。

## 当前安全边界

Read/Glob/Grep 默认允许；Bash 会对具体命令做保守的只读分析，能够证明安全的常见工作区查询（如 `pwd`、`ls`、`rg`、安全参数下的 `git status/diff/log`）无需确认，其余 Bash 与 Write/Edit 按 permission mode 决策。文件工具限制在工作区内，并保护 `.git/`、`.my-code/` 和 `claude-code/`。共享项目配置不能启用 `bypassPermissions`，避免仓库在缺少 workspace trust 流程时自行关闭权限边界。

Bash 的只读分析采用命令及参数白名单；重定向、命令替换、未加引号的 glob、工作区外路径、未知参数和无法可靠解析的语法都会回退到确认。它仍没有 OS 级 sandbox，不要对不可信提示使用 `--permission-mode bypassPermissions`。
