# Repository Guidelines

## Project Structure & Module Organization

The Python package lives in `agent_wrapper/`. Core approval flow is split across `approval.py`, `wrapper.py`, and `rules.py`; harness-specific integrations are under `agent_wrapper/runners/`. The multi-terminal HTTP/WebSocket server is `server.py`, with browser assets in `server_static/`. Tests are in `tests/`, design notes in `docs/`, and active plans and lessons in `tasks/`. Keep experimental integration probes in `agent_wrapper/sdk_prototype/`, not in production modules.

## Build, Test, and Development Commands

- `uv sync --frozen`: create or update the environment from `uv.lock`.
- `uv run pytest`: run the complete test suite.
- `uv run pytest tests/test_approval_broker.py -q`: run a focused test module.
- `uv run ruff format --check agent_wrapper tests`: verify formatting.
- `uv run ruff check agent_wrapper tests`: run lint checks.
- `uv run mypy agent_wrapper`: run static type checks.
- `uv run python -m agent_wrapper.server`: start the local multi-terminal server.

Use `sfw` for dependency installation and pin new packages to exact versions. Commit `uv.lock` with dependency changes.

## Coding Style & Naming Conventions

Use Python 3.12+, four-space indentation, type annotations, and Ruff formatting. Name modules, functions, and variables with `snake_case`; classes use `PascalCase`; constants use `UPPER_SNAKE_CASE`. Keep harness-specific protocol handling behind adapters and place shared approval policy in common modules. Avoid unrelated refactors and preserve UTF-8/LF line endings.

## Testing Guidelines

Tests use pytest with `unittest.TestCase` classes. Name files `test_<area>.py`, classes `<Feature>Tests`, and methods `test_<behavior>`. Add regression tests for every approval-state change, especially request-ID matching, duplicate responses, expiry, and cross-session isolation. Run the full test, format, lint, and type-check sequence before committing.

## Commit & Pull Request Guidelines

Follow the repository’s Conventional Commit style, such as `feat: add approval broker API`, `fix: reject stale request IDs`, or `docs: clarify server architecture`. Keep commits focused and do not commit failing tests. Pull requests should explain intent, affected harnesses, safety implications, and validation commands. Link relevant issues and include screenshots for dashboard changes.

## Security & Agent Workflow

Never bypass the approval broker or auto-select persistent “always allow” permissions. Treat installs, external writes, destructive commands, credentials, and scope expansion as human decisions. Before changes, review `tasks/todo.md`, `tasks/lessons.md`, and `tasks/alignment.md`; update them when responsibilities, terminology, or implementation locations change.
