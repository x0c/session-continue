# Contributing

Thanks for improving `pickup`.

## Development Setup

```bash
python3 -m pip install --user -e .
python3 -m unittest -v
```

The project keeps runtime dependencies minimal: the UI layer is built on [Textual](https://github.com/Textualize/textual) (the only required third-party package), everything else stays on the Python standard library.

## Before Opening A Pull Request

Run:

```bash
python3 -m py_compile pickup.py scan_claude.py scan_codex.py titles.py models.py runtime/*.py test_*.py
python3 -m unittest -v
```

For TUI changes, also run a real terminal smoke test. Avoid committing captured terminal output, local caches, or real Claude/Codex history files.

## Design Boundaries

- Keep runtime-specific behavior inside the matching adapter in `runtime/`.
- Keep `pickup.py` focused on UI, session display, user selection, and launch orchestration.
- Use native resume for the same runtime.
- Use source-runtime handoff data plus target-runtime launch plans for cross-runtime handoff.
- Do not rewrite or fabricate another runtime's private session files.

More detailed maintainer notes are in [docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md).
