# nano-code

一个用于学习 coding agent 架构的 Python 项目。它借鉴 Claude Code 的上下文、工具和权限边界，但以小型、可测试的 Python 实现表达，而不是完整复刻产品功能。

## 开发

```bash
uv sync --group dev
uv run nano-code --help
uv run nano-code
uv run nano-code -p "解释这个项目的结构"
```

运行需要 `ANTHROPIC_API_KEY`，可用 `ANTHROPIC_MODEL` 和 `ANTHROPIC_BASE_URL` 覆盖默认模型及 API 地址。首次开发可复制 `.env.example`，但程序不会自动加载 `.env`；应由 shell 或安全的密钥管理工具注入变量。

```bash
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest
```

架构阅读笔记从 [docs/README.md](docs/README.md) 开始；当前实现边界见 [docs/08-mvp-scope.md](docs/08-mvp-scope.md)。`claude-code/` 仅是本地只读参考源码，已被 `.gitignore` 排除，禁止提交或发布。

## 当前安全边界

Read/Glob/Grep 默认允许；Write/Edit/Bash 按 permission mode 决策。非交互模式无法显示确认框，因此默认拒绝需要询问的操作。文件工具限制在工作区内，并保护 `.git/`、`.nano-code/` 和 `claude-code/`。

Bash 目前只有权限确认，没有 OS 级 sandbox。不要对不可信提示使用 `--permission-mode bypassPermissions`。
