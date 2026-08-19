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
mycode -p "解释这个项目的结构"
```

`--editable` 适合本地开发；如果只想安装当前版本，可去掉该选项。卸载命令为
`uv tool uninstall my-code`。

## 开发

```bash
uv sync --group dev
uv run mycode --help
uv run mycode
uv run mycode -p "解释这个项目的结构"
```

首次使用可把 API Key 保存到用户级凭据文件：

```bash
mycode auth login
mycode auth status
```

Key 按 Provider 保存在 `~/.my-code/.credentials.json`，文件权限为 `0600`，不会进入项目配置或 Transcript。Anthropic-compatible 服务定义在用户级 `providers.json`；底层仍使用 Anthropic SDK。

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
mycode --provider company-gateway auth login
```

`MY_CODE_PROVIDER`、`MY_CODE_API_KEY`、`ANTHROPIC_BASE_URL`、`--provider` 和 `--base-url` 可用于临时覆盖。

交互模式使用 Textual 构建的组件化 TUI，支持流式 Markdown、工具调用及简略结果、运行状态和固定输入区。需要授权时，输入区会切换为 `Yes`、`No`、`No, and tell my-code why` 三项内联选择；拒绝原因会返回模型。在输入框键入 `/` 会显示候选命令，可用方向键选择、Tab 补全、Enter 执行；`/provider` 可安全配置 Provider URL、模型和 API Key，`/resume` 可选择并恢复当前项目的历史会话。单次 `-p` 模式不启动 TUI。

与 Claude Code 的目录边界一致，用户配置及运行状态默认放在 `~/.my-code/`，可用 `MY_CODE_CONFIG_DIR` 整体迁移。项目共享配置为 `.my-code/settings.json`，本地覆盖为已忽略的 `.my-code/settings.local.json`；读取配置不会主动创建这些目录。完整布局和优先级见 [docs/09-storage-and-settings.md](docs/09-storage-and-settings.md)，终端层边界见 [docs/10-terminal-ui.md](docs/10-terminal-ui.md)。

首次启动聊天或认证命令时会创建缺失的用户级 `settings.json`、`providers.json`、空 `.credentials.json` 和 `projects/`。Provider Profile 将 URL、协议和默认模型与凭据分离；当前所有 Profile 仍统一使用 Anthropic Messages API。

```bash
uv run ruff format .
uv run ruff check .
uv run pyright
uv run pytest
```

架构阅读笔记从 [docs/README.md](docs/README.md) 开始；当前实现边界见 [docs/08-mvp-scope.md](docs/08-mvp-scope.md)，提示词职责见 [docs/11-prompt-management.md](docs/11-prompt-management.md)。`claude-code/` 仅是本地只读参考源码，已被 `.gitignore` 排除，禁止提交或发布。

## 当前安全边界

Read/Glob/Grep 默认允许；Bash 会对具体命令做保守的只读分析，能够证明安全的常见工作区查询（如 `pwd`、`ls`、`rg`、安全参数下的 `git status/diff/log`）无需确认，其余 Bash 与 Write/Edit 按 permission mode 决策。非交互模式无法显示确认框，因此默认拒绝需要询问的操作。文件工具限制在工作区内，并保护 `.git/`、`.my-code/` 和 `claude-code/`。共享项目配置不能启用 `bypassPermissions`，避免仓库在缺少 workspace trust 流程时自行关闭权限边界。

Bash 的只读分析采用命令及参数白名单；重定向、命令替换、未加引号的 glob、工作区外路径、未知参数和无法可靠解析的语法都会回退到确认。它仍没有 OS 级 sandbox，不要对不可信提示使用 `--permission-mode bypassPermissions`。
