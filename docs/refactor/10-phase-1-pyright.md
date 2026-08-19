# 阶段 1：Pyright 迁移

完成日期：2026-08-19。

## 配置结果

- 开发依赖和锁文件只保留 Pyright 类型检查器。
- `pyproject.toml` 使用 `typeCheckingMode = "standard"`，检查 `src` 与 `tests`。
- 显式绑定项目 `.venv`，确保第三方依赖类型可被稳定解析。
- `.venv` 和只读参考源码 `claude-code` 不进入扫描范围。
- README、开发指南和现有架构文档统一使用 `uv run pyright`。

## 类型修正

- Session transcript 解码不再用分支内 tuple 展开构造 record，改为显式传递公共字段。
- Model contract 的公开符号由静态 `__all__` 明确声明。
- 测试对 SDK TypedDict、消息联合类型和 optional continuation 做显式收窄。
- TUI 测试 fake 补齐 `refresh_provider_models` 能力。

以上均为类型表达修正，不改变 transcript schema 或运行时行为。

## 验收

| 命令 | 结果 |
| --- | --- |
| `uv run ruff format --check .` | 通过，201 files already formatted |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 通过，0 errors、0 warnings |
| `uv run pytest` | 通过，349 tests passed |

仓库源码、配置、文档和锁文件中不再保留旧类型检查器引用。
