# Privacy

`session-continue` is designed as a local terminal utility for existing Claude Code and Codex CLI users.

## Data It Reads

- Claude Code history under `~/.claude/projects/`.
- Codex CLI history under `~/.codex/sessions/`.
- Codex session names from `~/.codex/session_index.jsonl` when present.

The tool reads these files to build a recent-session list, extract a compact preview, and prepare native resume or cross-runtime handoff commands.

## Data It Writes

- Generated title cache under `~/.cache/session-continue/titles.json`.
- A lock file under `~/.cache/session-continue/titles.lock` while title generation is running.

It does not write to Claude Code or Codex CLI history files.

## Network And Account Usage

The core scanner, TUI, preview screen, and JSON output do not make network requests by themselves.

Optional title generation launches your local `claude` command. That command may contact Anthropic services according to your Claude Code setup and may consume account quota. If the command is missing or fails, the tool keeps using local fallback titles.

When you resume or hand off a session, the selected runtime process takes over the terminal. From that point on, Claude Code or Codex CLI behaves according to its own configuration.

## Cross-Runtime Handoff

For handoff between runtimes, the tool passes the original history file path and a short format hint to the target runtime. It does not copy the full conversation into command-line arguments and does not modify the source session.

The target runtime may choose to read that local history file after it starts.

## Repository Hygiene

Do not commit real session history, generated caches, logs, tokens, API keys, or local environment files. The project `.gitignore` excludes common local artifacts, but contributors should still review changes before publishing.
