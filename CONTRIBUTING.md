# Contributing

Thanks for improving `pickup`.

## Development Setup

```bash
python3 -m pip install --user -e .
python3 -m unittest discover -s tests -v
```

The project keeps runtime dependencies minimal: the UI layer is built on [Textual](https://github.com/Textualize/textual) (the only required third-party package), everything else stays on the Python standard library.

## Before Opening A Pull Request

Run:

```bash
python3 -m compileall -q src/pickup tests
python3 -m unittest discover -s tests -v
```

For TUI changes, also run a real terminal smoke test. Avoid committing captured terminal output, local caches, or real Claude/Codex history files.

## Design Boundaries

- Keep runtime-specific behavior inside the matching adapter in `src/pickup/runtime/`.
- Keep `cli` / `store` / `display` / `theme` focused on entry, session display, user selection, and launch orchestration — do not assemble per-runtime argv outside adapters.
- Use native resume for the same runtime.
- Use source-runtime handoff data plus target-runtime launch plans for cross-runtime handoff.
- Do not rewrite or fabricate another runtime's private session files.

More detailed maintainer notes are in [docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md).
