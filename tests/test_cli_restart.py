"""pickup.cli._restart_process：客户端自动更新重启后用新代码 re-exec 自身。

os.execv 会立即替换当前进程、不会返回，测试里必须 mock 掉，否则会杀死测试进程。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from pickup import cli


class RestartProcessTests(unittest.TestCase):
    def test_reexecs_with_same_interpreter_and_passthrough_argv(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["pickup", "--limit", "5"]), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(
            sys.executable, [sys.executable, "-m", "pickup", "--limit", "5"],
        )

    def test_reexecs_with_no_extra_args(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["pickup"]), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "pickup"])


if __name__ == "__main__":
    unittest.main()
