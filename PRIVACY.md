# Privacy

`pickup` is designed as a local terminal utility for existing Claude Code, Codex CLI, OpenCode, and Kimi Code CLI users.

## Data It Reads

- Claude Code history under `~/.claude/projects/`.
- Codex CLI history under `~/.codex/sessions/`.
- Codex session names from `~/.codex/session_index.jsonl` when present.
- OpenCode history from its SQLite database at `~/.local/share/opencode/opencode.db` (or the
  directory pointed to by `OPENCODE_DATA_DIR`), opened with a read-only connection (`mode=ro`).
  The tool never writes to this database.
- Kimi Code CLI history under `~/.kimi-code/sessions/` (per-session `state.json` metadata and the
  `agents/main/wire.jsonl` conversation log).

The tool reads these files to build a recent-session list, extract a compact preview, and prepare native resume or cross-runtime handoff commands.

## Data It Writes

- Generated title cache under `~/.cache/pickup/titles.json`.
- A lock file under `~/.cache/pickup/titles.lock` while title generation is running.

It does not write to Claude Code, Codex CLI, OpenCode, or Kimi Code CLI history.

## Network And Account Usage

The core scanner, TUI, preview screen, and JSON output do not make network requests by themselves.

Optional title generation launches one of your locally installed agent CLIs (`claude` or `codex`; auto-detected, or pinned via `PICKUP_TITLE_GENERATOR`, legacy name `SC_TITLE_GENERATOR`). That command sends short session excerpts to the corresponding model provider under your own account and credentials. If the command is missing or fails, the tool keeps using local fallback titles.

When you resume or hand off a session, the selected runtime process takes over the terminal. From that point on, Claude Code or Codex CLI behaves according to its own configuration.

## Keep-Alive (Background tmux)

By default, sessions started or resumed from the TUI are wrapped in a dedicated background `tmux`
server (socket name `pickup-keepalive`) so the underlying process survives an SSH disconnect. This changes
what stays running after `pickup` exits:

- The wrapped runtime process (and everything it does) keeps running in the background until it exits
  on its own, is manually closed (`x` in the TUI), or is auto-reaped after being idle (default 24h, see
  `PICKUP_KEEPALIVE_IDLE_HOURS`, legacy name `SC_KEEPALIVE_IDLE_HOURS`).
- To detect which sessions are already backgrounded, `pickup` reads the local process table (`ps -eo
  pid,ppid`) and lists the tmux server's own sessions (`tmux -L pickup-keepalive list-sessions`). This is
  local process metadata, not file content, and is not written anywhere.
- On a machine shared with other local users, anyone able to run commands as your OS user (or root) can
  attach to `tmux -L pickup-keepalive` and see the live terminal content of a backgrounded session — the
  same exposure any tmux session already has under your account; `pickup` does not add encryption or
  access control on top of it.
- Disable entirely with `pickup --no-keepalive` for one run, or `PICKUP_KEEPALIVE=0` (legacy
  `SC_KEEPALIVE=0`) permanently. The full-screen attach form is skipped when `pickup` is already
  running inside `tmux`/`screen`; embedded panes don't attach and are unaffected.

## Cross-Runtime Handoff

For handoff between runtimes, the tool passes the original history location (a file path, or a
SQLite database path plus session ID for OpenCode) and a short format hint to the target runtime.
It does not copy the full conversation into command-line arguments and does not modify the source
session.

The target runtime may choose to read that local history after it starts.

## Repository Hygiene

Do not commit real session history, generated caches, logs, tokens, API keys, or local environment files. The project `.gitignore` excludes common local artifacts, but contributors should still review changes before publishing.
