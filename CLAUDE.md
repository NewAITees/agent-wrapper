# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two independent, separately-invoked tools that share one Python package (`agent_wrapper/`):

1. **`agent-wrapper`** (`main.py`) — an approval-gated wrapper that runs Claude Code or Codex
   headlessly and routes every dangerous operation through a rules regex, an Ollama content
   triage, and a human dashboard approval before it executes.
2. **`agent-wrapper-server`** (`server.py`) — a separate, newer tool that runs claude/codex/opencode
   in raw ConPTY terminals exposed to a browser over WebSocket, with no approval gate — the human
   answers each harness's own interactive prompts directly. See "Two tools, not one" below before
   touching either.

## Commands

```bash
uv sync                                   # install/sync dependencies
uv run pytest                             # full test suite
uv run pytest tests/test_server.py        # one file
uv run pytest tests/test_server.py::BuildArgvTests::test_claude_with_model  # one test (unittest classes, pytest runner)
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy .                             # type check
uv tool install -e . --reinstall          # reinstall the CLI shims after changing deps or [project.scripts]
agent-wrapper --agent mock --checkin-interval 10   # run the approval-gated wrapper against the mock agent
agent-wrapper-server                      # run the raw-pty multi-terminal web server
```

Both console scripts require `uv tool install -e .` once (see README.md); after that they're on PATH
and editable installs pick up code changes without reinstalling — reinstall only when dependencies or
`[project.scripts]` change.

## Two tools, not one

Do not assume the approval gate (`rules.py` / `ollama_client.py` / `SharedState`) applies to
`server.py`'s ConPTY sessions — it currently doesn't. They are deliberately separate systems
(`tasks/alignment.md` calls these session kinds `wrapped` vs. `raw-pty`); connecting them is an
open, unimplemented design problem tracked in `tasks/todo.md` ("orchestrator / utility ロール導入"),
not something to assume is already wired up.

## Architecture: `agent-wrapper` (approval-gated wrapper)

Three-layer gate, applied to every tool call / shell command an agent tries to run:

1. `rules.check_destructive()` — regex allowlist in `rules.py` (`DESTRUCTIVE_PATTERNS`). A match
   (rm -rf, force push, package installs, deploys, secret changes, ...) **always** escalates to a
   human, bypassing Ollama entirely. This is intentional: Ollama's triage is a convenience layer for
   the ambiguous middle, not a substitute for hard-coded denials.
2. `ollama_client.judge_permission()` — first-pass triage by a local Ollama model (default
   `gemma4:e4b`, configurable via `--ollama-model`) for everything the regex didn't already escalate.
   Connection or model failure fails safe (escalates to human, never auto-approves).
3. Human approval via the Flask dashboard (`dashboard.py`) — a single-page app polling `/status`
   and `/log`, responding through `/respond`. Approval requests are FIFO-queued in `SharedState`
   (`wrapper.py`) with per-request `request_id`s specifically to avoid one approval releasing an
   unrelated pending request (`tasks/lessons.md` documents this bug happening with a single shared
   `threading.Event`).

Two agent backends share this gate through a common base:

- `runners/base.py` (`ApprovalRunnerBase`) — the `_gate()` / `_conversational_reply()` logic every
  runner calls into; also owns the plan-approval flow (`PLAN_PROMPT_PREFIX` forces the agent to
  present a plan before acting) and the conversational-turn limit (which escalates to a human
  instead of unilaterally cutting off the session — see `tasks/lessons.md` 2026-07-11).
- `runners/claude_runner.py` (`ClaudeRunner`) — drives real Claude Code via `claude-agent-sdk`.
  `PreToolUse` always returns `"ask"`; the actual allow/deny decision happens in `can_use_tool`,
  because `PreToolUse` alone can't see tool calls Claude itself judges "safe" (see module docstring
  for the GitHub issue reference). Uses `setting_sources=["project"]` to avoid the *user's* global
  `~/.claude/CLAUDE.md` leaking into the wrapped session.
- `runners/codex_runner.py` (`CodexRunner`) — drives Codex via `codex mcp-server` as an MCP client,
  `approval-policy=untrusted`. Codex's elicitation response is a nonstandard top-level
  `{"decision": "approved"|"denied"}` shape, not MCP's standard `action`/`content` — see the module
  docstring. **Known risk**: `codex mcp-server` is now deprecated upstream and its `approval-policy`
  enum has changed since this was written (`tasks/todo.md`); verify against the installed `codex`
  version before trusting this path. `--codex-sandbox` defaults to `danger-full-access` — this is a
  deliberate choice (`docs/agent_wrapper_permission_policy.md` §8), not an oversight: the approval
  gate is the actual safety boundary regardless of sandbox setting, and Windows sandbox mode has its
  own upstream bugs (openai/codex#21982) that have stalled real work. Don't add
  `--codex-sandbox workspace-write` unless a user explicitly asks for it.

`ollama_client.py` also classifies destructive matches and judges whether an agent's free-text
question is actually asking for permission (`judge_conversational_question`) vs. just talking —
tool-call safety never depends on this conversational judgment; it's a separate, softer layer.

## Architecture: `agent-wrapper-server` (raw-pty web server)

`server.py` manages named `Session` objects (one ConPTY process each, via `pywinpty`), each with a
`scrollback` buffer and a set of WebSocket `subscribers` — sessions are created once via `POST /start`
and shared by every viewer that connects, not re-spawned per connection (a prior bug: see the
"セッション共有" note in `tasks/lessons.md` / commit history — two browser tabs used to get two
independent processes).

HTTP endpoints (`http.server.ThreadingHTTPServer`, not Flask): `GET /pick-folder` (native Tk folder
dialog — the browser can't return an absolute filesystem path itself), `GET /models?harness=`,
`GET /recent-folders`, `GET /sessions`, `POST /start`, `POST /send/<name>`. WebSocket endpoint is
`ws://127.0.0.1:8766/<name>`.

Windows-specific gotchas baked into this file, don't "simplify" them away:
- `resolve_opencode_exe()` bypasses the npm-generated `opencode.cmd`/`opencode.ps1` shims, which are
  broken on this setup (they assume `/bin/sh` is available) — it resolves the real
  `opencode-windows-x64/bin/opencode.exe` via `npm root -g` instead.
- That `npm root -g` call itself has to go through `cmd /c` because `npm` is `npm.cmd`, and
  `subprocess`/`CreateProcess` won't launch a `.cmd` file directly.
- opencode's model list is queried live (`opencode models`, parsed by `parse_opencode_models`) rather
  than hardcoded, and merged with Ollama's own `/api/tags` for the `ollama` source specifically —
  opencode's local config only lists whatever the user manually registered in `opencode.jsonc`, which
  undercounts what's actually pulled.
- claude/codex have no CLI command to list models, so their source/model options are a small
  hardcoded dict (`_STATIC_MODELS`) — expect this to go stale.

See `docs/agent_server_architecture_options.md` for why this is a self-built ConPTY+WebSocket
approach rather than wrapping each harness's own native background/daemon feature (`claude --bg`,
`codex app-server`, `opencode serve`) — the short version: those features are either local-terminal-only
(so "operate over the web" would need this same bridging layer anyway) or unstable/experimental on
this setup, which defeats the point of a wrapper meant to paper over exactly that instability.

## Working in this repo

- `tasks/alignment.md` — term definitions (`harness`, `wrapped`/`native-bg`/`raw-pty` session kinds,
  `Approval Broker`) and a dated misalignment log; check before assuming what a term means.
- `tasks/lessons.md` — dated bug/lesson log; check before re-implementing something that already
  bit someone (threading/approval races, Windows subprocess quirks, opencode's self-updating and
  Windows shims, delegated-agent work that self-reports success without actually applying).
- `tasks/todo.md` — open work and unresolved design decisions; sections get deleted once fully checked off.
- `docs/agent_wrapper_permission_policy.md` — the L0–L3 permission-level policy the approval gate
  implements, and *why* (includes real incident data from running this against actual coding tasks).
- `skills/delegate/SKILL.md` — how another Claude Code session should invoke `agent-wrapper` to
  delegate a task to it (detached process, no polling/monitoring, human approves via dashboard,
  reviewer must re-run tests/lint itself rather than trust the delegate's completion report).
- `agent_wrapper/sdk_prototype/` is scratch/throwaway exploration scripts (not imported by either
  tool) — don't treat code there as production, and don't assume it stays around.
- Package installs go through `sfw` (Socket Firewall) with exact-pinned versions in `dependencies`
  (never `^`/`>=`/`latest`), per the user's global tooling convention.
