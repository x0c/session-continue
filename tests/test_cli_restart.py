"""pickup.cli._restart_process：客户端自动更新重启后用新代码 re-exec 自身。

os.execv 会立即替换当前进程、不会返回，测试里必须 mock 掉，否则会杀死测试进程。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from pickup import cli


class RestartProcessTests(unittest.TestCase):
    def test_reexecs_via_path_pickup_when_available(self) -> None:
        # brew 升级后 PATH 上的 pickup 软链已指向新版本，优先用它重启
        with mock.patch.object(cli.sys, "argv", ["pickup", "--limit", "5"]), \
             mock.patch("shutil.which", return_value="/opt/homebrew/bin/pickup"), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(
            "/opt/homebrew/bin/pickup", ["/opt/homebrew/bin/pickup", "--limit", "5"],
        )

    def test_falls_back_to_interpreter_module_when_no_path_binary(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["pickup"]), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "pickup"])


if __name__ == "__main__":
    unittest.main()
