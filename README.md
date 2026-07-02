# session-continue

[![test](https://github.com/x0c/session-continue/actions/workflows/test.yml/badge.svg)](https://github.com/x0c/session-continue/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Fast terminal session picker for Claude Code and Codex CLI.

`session-continue` scans your local Claude Code and Codex CLI history, shows recent coding sessions in a curses TUI, and lets you resume the selected session in its native runtime. It can also hand off a Claude session to Codex, or a Codex session to Claude, by starting a new target session with a structured pointer to the original JSONL history.

Keywords: Claude Code session manager, Codex CLI resume, terminal TUI, AI coding agent workflow, JSONL chat history, cross-runtime handoff.

## Why Use It

- Browse recent Claude Code and Codex CLI sessions from one terminal screen.
- Resume with the original runtime using native commands such as `claude --resume` and `codex resume`.
- Preview the user messages and final assistant replies before resuming.
- Hand off unfinished work between runtimes without rewriting or faking session files.
- Keep generated titles in a local cache so repeat launches stay fast.
- Use JSON output for scripts and launchers.

## Privacy Model

The tool is local-first.

- It reads local history files under `~/.claude/projects/` and `~/.codex/sessions/`.
- It does not upload session history by itself.
- Cross-runtime handoff passes the original history file path to the target runtime instead of copying the whole conversation into command-line arguments.
- Optional title generation calls your installed `claude` command and may consume Claude account quota.
- Title cache files are stored under `~/.cache/session-continue/`.

See [PRIVACY.md](PRIVACY.md) for the detailed privacy and data-flow notes.

## Requirements

- Python 3.10 or newer.
- macOS or Linux terminal with curses support.
- Claude Code and/or Codex CLI installed if you want to resume those sessions.

## Install

### Homebrew (macOS/Linux)

```bash
brew install x0c/tap/session-continue
```

### Install Script

```bash
curl -fsSL https://raw.githubusercontent.com/x0c/session-continue/main/install.sh | bash
```

Requires Python 3.10+. Installs via `pip install --user` and prints a `PATH` hint if the install directory isn't already on it.

### From Source

```bash
git clone https://github.com/x0c/session-continue.git
cd session-continue
python3 -m pip install --user .
```

Then run:

```bash
sc
```

### Without Installing

```bash
git clone https://github.com/x0c/session-continue.git
cd session-continue
python3 sc.py
```

You can also add a symlink manually:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/sc.py" ~/.local/bin/sc
chmod +x sc.py
```

Make sure `~/.local/bin` is in your `PATH`.

## Usage

```bash
sc                  # open the interactive TUI
sc --limit 30       # show up to 30 sessions per runtime
sc --json           # print sessions as JSON and exit
sc --json --limit 5 # script-friendly small result set
```

JSON output includes runtime, session ID, title, working directory, update time, size, status, resume command, and history path.

## Agent / Automation

`sc` also exposes read-only, structured subcommands meant for AI agents to query local session
history — list, search, inspect, and build a handoff context package. None of them launch or
resume anything; what to do with the data is left to the caller.

```bash
sc list --cwd my-app --status pending   # structured session list, filterable
sc search weather app --deep            # find sessions by topic (title/messages/cwd)
sc show <session-id-prefix>             # session detail + conversation
sc context <session-id-prefix>          # handoff package: history path, suggested prompt, resume command
sc describe [command]                   # machine-readable command/argument/field reference
```

Every command prints a JSON envelope (`{ok, data, error, meta}`) and uses fine-grained exit codes
(`0` success, `2` usage error, `3` not found, `5` ambiguous session reference). Running `sc` with no
subcommand outside a real terminal (piped, scripted, or invoked by an agent) also falls back to a
JSON session list instead of trying to start the curses TUI.

See [docs/SKILL.md](docs/SKILL.md) for the full command reference, field semantics, and typical
agent workflows.

## Key Bindings

| Key | Action |
| --- | --- |
| `Up` / `Down` / `j` / `k` | Move selection |
| `Left` / `Right` / `Tab` | Switch runtime column |
| `Space` | Open full-screen conversation preview |
| `Home` / `End` | Jump in preview |
| `Enter` | Resume selected session with the native runtime |
| `a` | Open advanced handoff actions |
| `q` | Close preview/dialog or quit |

`Esc` is intentionally not used as the quit key because terminals also use escape sequences for arrow keys.

## Cross-Runtime Handoff

Native resume is used when the source and target runtime are the same.

When the target runtime is different, `session-continue` creates a new session in the target runtime. The prompt includes:

- source runtime name;
- original session title;
- original working directory;
- original JSONL history path;
- a short format hint for reading that history.

The original session file is left untouched. The target runtime decides what history it needs to read before continuing the work.

## Title Generation

The TUI first shows a local fallback title so the first screen is immediate. A detached background process can then generate better Chinese titles in small batches through `claude -p --model haiku`.

Cost controls:

- generated titles are cached by runtime and session ID;
- a file lock prevents duplicate title-generation workers;
- failures keep the local fallback title instead of retrying every item slowly.

Title generation is optional in practice: if `claude` is unavailable or fails, the session picker still works.

## Project Layout

| Path | Purpose |
| --- | --- |
| `sc.py` | curses TUI, preview screen, JSON output, and process handoff |
| `agent_api.py` | read-only `list`/`search`/`show`/`context`/`describe` subcommands for agents |
| `models.py` | shared session, handoff, and launch-plan data models |
| `runtime/` | runtime adapters for scanning, native resume, and new-session launch |
| `scan_claude.py` | Claude Code history scanner |
| `scan_codex.py` | Codex CLI history scanner |
| `titles.py` | local title fallback, cache, status labels, and batch generation |
| `docs/SKILL.md` | agent-facing command reference (SKILL.md convention) |
| `test_*.py` | unit tests |

## Development

Run the same checks used by CI:

```bash
python3 -m py_compile sc.py scan_claude.py scan_codex.py titles.py models.py runtime/*.py test_*.py
python3 -m unittest -v
```

For UI changes, run a real terminal smoke test as well.

Maintainer notes live in [AGENTS.md](AGENTS.md) and [docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md).

## License

MIT. See [LICENSE](LICENSE).
