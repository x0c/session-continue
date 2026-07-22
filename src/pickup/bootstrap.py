"""极轻量命令分发：快速命令不加载 Textual、网络更新器或会话扫描器。"""

from __future__ import annotations

import os
import sys

from pickup import __version__

_AGENT_ROOTS = {"list", "search", "show", "context", "plan", "describe", "diagnose"}


def _fast_version() -> None:
    import pickup

    package_file = os.path.abspath(pickup.__file__ or "")
    print(f"pickup {__version__}")
    print(f"  package_file: {package_file}")
    print(f"  python:       {sys.executable}")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V", "-v"}:
        _fast_version()
        return
    if argv[:1] == ["cache"]:
        from pickup.cache_cli import main as cache_main

        raise SystemExit(cache_main(argv[1:]))
    if argv and argv[0] in _AGENT_ROOTS:
        from pickup.agent_api import dispatch

        raise SystemExit(dispatch(argv))
    if argv[:1] == ["update"]:
        from pickup.updater import cli_update

        raise SystemExit(cli_update())
    from pickup.cli import main as cli_main

    cli_main()
