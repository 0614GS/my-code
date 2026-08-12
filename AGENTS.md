# Repository Guidelines

## Project Structure & Module Organization

Production code lives under `src/nano_code/`. Domain packages include `agent/`, `context/`, `messages/`, `sessions/`, `tools/`, `permissions/`, and `providers/`; `cli/` owns terminal interaction. Put built-ins in `tools/builtin/`. Mirror domains under `tests/unit/`; reserve `tests/integration/` for cross-component workflows. Architecture notes live in `docs/`. `claude-code/` is reference material.

## Build, Test, and Development Commands

Use uv with Python 3.12 or newer:

- `uv sync` creates or updates the project `.venv`.
- `uv run nano-code --help` checks the installed CLI entry point.
- `uv run nano-code -p "prompt"` runs one non-interactive turn.
- `uv run ruff format .` formats Python files.
- `uv run ruff check .` runs lint checks.
- `uv run mypy src` performs static type checking.
- `uv run pytest` runs the test suite.

Run `uv sync --group dev` before these commands. The CLI requires `ANTHROPIC_API_KEY` for live model requests.

## Coding Style & Naming Conventions

Use four-space indentation and complete type annotations; mypy compliance provides TypeScript-like guarantees. Use `snake_case` for modules and functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Avoid catch-all `utils` modules. Keep code files below 1,000 lines; near that limit, split responsibilities before adding behavior.

## Testing Guidelines

Use pytest naming: files `test_<subject>.py`, test functions `test_<behavior>()`. Add unit tests beside the mirrored domain and regression tests for every bug fix. Permission, context-compaction, transcript-recovery, and tool-execution changes should cover failure and cancellation paths. No numeric coverage threshold exists yet; prioritize meaningful behavioral coverage.

## Commit & Pull Request Guidelines

No historical convention exists yet. Use focused, imperative Conventional Commits such as `feat: add tool registry`. Pull requests should explain motivation and behavior, list verification commands, link issues, and update architecture notes when boundaries or invariants change. Include terminal output only for material CLI changes.

## Security & Configuration

Never commit API keys, local transcripts, generated tool outputs, or `.venv`. Treat permission and sandbox changes as security-sensitive and require explicit tests for deny and bypass cases.

## Reference-Source Workflow

Before changing a core mechanism, inspect its implementation under the ignored `claude-code/` snapshot and the notes in `docs/`. Preserve invariants instead of translating mechanically. Record intentional differences in `docs/08-mvp-scope.md`. Never stage, package, or publish the snapshot.
