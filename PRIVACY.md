# Privacy

`session-continue` is designed as a local terminal utility for existing Claude Code, Codex CLI, and OpenCode users.

## Data It Reads

- Claude Code history under `~/.claude/projects/`.
- Codex CLI history under `~/.codex/sessions/`.
- Codex session names from `~/.codex/session_index.jsonl` when present.
- OpenCode history from its SQLite database at `~/.local/share/opencode/opencode.db` (or the
  directory pointed to by `OPENCODE_DATA_DIR`), opened with a read-only connection (`mode=ro`).
  The tool never writes to this database.

The tool reads these files to build a recent-session list, extract a compact preview, and prepare native resume or cross-runtime handoff commands.

## Data It Writes

- Generated title cache under `~/.cache/session-continue/titles.json`.
- A lock file under `~/.cache/session-continue/titles.lock` while title generation is running.

It does not write to Claude Code, Codex CLI, or OpenCode history.

## Network And Account Usage

The core scanner, TUI, preview screen, and JSON output do not make network requests by themselves.

Optional title generation launches one of your locally installed agent CLIs (`claude` or `codex`; auto-detected, or pinned via `SC_TITLE_GENERATOR`). That command sends short session excerpts to the corresponding model provider under your own account and credentials. If the command is missing or fails, the tool keeps using local fallback titles.

When you resume or hand off a session, the selected runtime process takes over the terminal. From that point on, Claude Code or Codex CLI behaves according to its own configuration.

## Keep-Alive (Background tmux)

By default, sessions started or resumed from the TUI are wrapped in a dedicated background `tmux`
server (socket name `sc-keepalive`) so the underlying process survives an SSH disconnect. This changes
what stays running after `sc` exits:

- The wrapped runtime process (and everything it does) keeps running in the background until it exits
  on its own, is manually closed (`x` in the TUI), or is auto-reaped after being idle (default 24h, see
  `SC_KEEPALIVE_IDLE_HOURS`).
- To detect which sessions are already backgrounded, `sc` reads the local process table (`ps -eo
  pid,ppid`) and lists the tmux server's own sessions (`tmux -L sc-keepalive list-sessions`). This is
  local process metadata, not file content, and is not written anywhere.
- On a machine shared with other local users, anyone able to run commands as your OS user (or root) can
  attach to `tmux -L sc-keepalive` and see the live terminal content of a backgrounded session — the
  same exposure any tmux session already has under your account; `sc` does not add encryption or
  access control on top of it.
- Disable entirely with `sc --no-keepalive` for one run, or `SC_KEEPALIVE=0` permanently. Skipped
  automatically when `tmux` is not installed or `sc` is already running inside `tmux`/`screen`.

## Cross-Runtime Handoff

For handoff between runtimes, the tool passes the original history location (a file path, or a
SQLite database path plus session ID for OpenCode) and a short format hint to the target runtime.
It does not copy the full conversation into command-line arguments and does not modify the source
session.

The target runtime may choose to read that local history after it starts.

## Repository Hygiene

Do not commit real session history, generated caches, logs, tokens, API keys, or local environment files. The project `.gitignore` excludes common local artifacts, but contributors should still review changes before publishing.
