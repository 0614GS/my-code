# 仓库开发指南

## 项目结构与模块组织

生产代码位于 `src/my_code/`。顶层领域包由 `tach.toml` 独立建模；`application/` 由 `contracts`、`turns`、`sessions`、`configuration` 和 `activity` 组成，`ApplicationService` 是 TUI façade 与 operation-lock 所有者。`features/` 仅包含 `background_tasks`、`subagents` 和 `todos` 三个纵向能力，Question 与文件 mention 由 `application/turns` 拥有。`cli/` 与 `tui/` 负责终端交互，内置工具放在 `tools/builtin/`。单元测试按领域镜像到 `tests/unit/`，跨组件流程放在 `tests/integration/`，源码级所有权守卫放在 `tests/architecture/`。架构文档位于 `docs/`，`claude-code/` 仅作为参考源码。

## 构建、测试与开发命令

使用 uv 和 Python 3.12 或更高版本：

- `uv sync`：创建或更新项目的 `.venv`。
- `uv run mycode --help`：检查 CLI 入口。
- `uv run mycode`：启动交互式 TUI。
- `uv run python scripts/check.py`：依次检查格式、代码规范、类型、模块依赖和测试。
- `uv run pytest <path>`：开发期间运行与改动直接相关的目标测试。

首次开发前运行 `uv sync --group dev`。真实模型请求需要已配置的 provider API key，也可用 `ANTHROPIC_API_KEY` 临时覆盖。

## 开发规范

命名、注释、docstring、测试和 Commit 规范统一维护在 `CONTRIBUTING.md`。代码内注释与 docstring 使用中文，解释原因、约束和非显然行为，不复述代码。修改历史英文注释所在代码时渐进转换，不做无关的批量翻译。

## 测试规范

单元测试按 `src/my_code/` 镜像到 `tests/unit/<module>/`，不得直接散落在 `tests/unit` 根目录。跨领域协作流程放在 `tests/integration/`，源码所有权守卫放在 `tests/architecture/`。临时实验不得进入测试套件。每个缺陷修复都要增加回归测试；权限、上下文压缩、会话恢复和工具执行的改动必须覆盖失败与取消路径。

## Commit 规范

使用聚焦、祈使语气的 Conventional Commits，例如 `feat(context): 支持上下文快照`。Agent 仅在用户明确要求时创建 commit，并且不得包含任务范围外的文件。

## 安全与配置

禁止提交 API key、本地会话记录、生成的工具输出、`.my-code/settings.local.json` 或 `.venv`。共享的 `.my-code/settings.json` 可以提交。权限和沙箱改动属于安全敏感变更，必须显式测试拒绝与绕过场景。

## 参考源码工作流

修改核心机制前，先查看被忽略的 `claude-code/` 源码快照及 `docs/` 中的说明。学习并保持架构不变量，不做机械翻译；有意保留的差异记录到 `docs/09-current-scope.md`。禁止暂存、打包或发布该源码快照。

## Agent 工作约束

修改前检查工作树并保留用户已有改动。先确认领域所有者，不创建含混的公共模块或跨所有者 re-export；不以顺手重构扩大任务范围。行为变化同步测试，架构不变量变化同步文档和 Tach 声明。完成前运行与改动匹配的目标检查，再运行完整本地质量门；不得声称未实际运行的检查已经通过。未经明确要求，不 commit、push、改写历史或删除用户文件。
