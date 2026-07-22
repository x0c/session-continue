"""pickup 顶层通用 CLI 参数回归测试。"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from pickup import cli


class CommonCliOptionsTests(unittest.TestCase):
    def _run_version(self, *options: str) -> str:
        report = {
            "version": "1.2.3",
            "package_file": "/tmp/pickup/__init__.py",
            "python": "/usr/bin/python3",
            "channel": "test",
            "checkout_root": None,
            "loaded_from_checkout": False,
            "stale_source_warning": None,
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["pickup", *options]),
            mock.patch.object(cli.observe, "install_crash_hooks"),
            mock.patch.object(cli, "default_registry"),
            mock.patch.object(cli.updater, "install_report", return_value=report),
            redirect_stdout(stdout),
        ):
            cli.main()
        return stdout.getvalue()

    def test_version_supports_common_short_and_long_aliases(self) -> None:
        for option in ("-v", "-V", "--version"):
            with self.subTest(option=option):
                self.assertTrue(self._run_version(option).startswith("pickup 1.2.3\n"))

    def test_debug_and_verbose_enable_diagnostics(self) -> None:
        for option in ("-d", "--debug", "--verbose"):
            with self.subTest(option=option), mock.patch.dict(os.environ, {}, clear=True):
                self._run_version(option, "--version")
                self.assertEqual(os.environ.get("PICKUP_DEBUG"), "1")

    def test_quiet_takes_precedence_over_debug(self) -> None:
        with mock.patch.dict(os.environ, {"PICKUP_DEBUG": "1"}, clear=True):
            self._run_version("--debug", "--quiet", "--version")
            self.assertNotIn("PICKUP_DEBUG", os.environ)

    def test_no_color_sets_standard_environment_switch(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self._run_version("--no-color", "--version")
            self.assertEqual(os.environ.get("NO_COLOR"), "1")

    def test_no_input_uses_noninteractive_json_path(self) -> None:
        registry = SimpleNamespace(ids=())
        with (
            mock.patch.object(sys, "argv", ["pickup", "--no-input", "--limit", "7"]),
            mock.patch.object(cli.observe, "install_crash_hooks"),
            mock.patch.object(cli, "default_registry", return_value=registry),
            mock.patch.object(cli, "_output_json") as output_json,
        ):
            cli.main()
        output_json.assert_called_once_with(registry, 7)


if __name__ == "__main__":
    unittest.main()
