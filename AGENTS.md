# 仓库开发指南

## 项目结构与模块组织

生产代码位于 `src/nano_code/`。核心领域包包括 `agent/`、`config/`、`context/`、`messages/`、`sessions/`、`tools/`、`permissions/` 和 `providers/`；`cli/` 负责终端交互。内置工具放在 `tools/builtin/`。单元测试按领域镜像到 `tests/unit/`，跨组件流程放在 `tests/integration/`。架构文档位于 `docs/`，`claude-code/` 仅作为参考源码。

## 构建、测试与开发命令

使用 uv 和 Python 3.12 或更高版本：

- `uv sync`：创建或更新项目的 `.venv`。
- `uv run nanocode --help`：检查 CLI 入口。
- `uv run nanocode -p "prompt"`：执行一次非交互式对话。
- `uv run ruff format .`：格式化 Python 文件。
- `uv run ruff check .`：执行代码规范检查。
- `uv run pyright`：以 standard 模式执行静态类型检查。
- `uv run pytest`：运行测试套件。

首次开发前运行 `uv sync --group dev`。真实模型请求需要已配置的 provider API key，也可用 `ANTHROPIC_API_KEY` 临时覆盖。

## 代码风格与命名约定

使用四空格缩进和完整类型标注，并通过 Pyright standard 获得静态类型保障。模块与函数使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。避免职责含混的 `utils` 模块。单个代码文件一般不得超过 1,000 行；接近上限时，应先重新划分职责和目录结构。

## 测试规范

使用 pytest 命名：文件为 `test_<subject>.py`，函数为 `test_<behavior>()`。单元测试应放在对应领域目录；每个缺陷修复都要增加回归测试。权限、上下文压缩、会话恢复和工具执行的改动必须覆盖失败与取消路径。目前不设硬性覆盖率指标，优先验证有意义的行为。

## 提交与 Pull Request 规范

当前没有既有提交规范。使用聚焦、祈使语气的 Conventional Commits，例如 `feat: add tool registry`。Pull Request 应说明动机与行为变化、列出验证命令并关联 issue；架构边界或不变量变化时同步更新文档。仅在 CLI 有实质变化时附终端输出。

## 安全与配置

禁止提交 API key、本地会话记录、生成的工具输出、`.nano-code/settings.local.json` 或 `.venv`。共享的 `.nano-code/settings.json` 可以提交。权限和沙箱改动属于安全敏感变更，必须显式测试拒绝与绕过场景。

## 参考源码工作流

修改核心机制前，先查看被忽略的 `claude-code/` 源码快照及 `docs/` 中的说明。学习并保持架构不变量，不做机械翻译；有意保留的差异记录到 `docs/08-mvp-scope.md`。禁止暂存、打包或发布该源码快照。
