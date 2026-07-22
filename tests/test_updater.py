"""pickup.updater：版本比较、安装渠道判定、最新版查询、忽略状态持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from pickup import updater


class VersionCompareTests(unittest.TestCase):
    def test_version_tuple_strips_leading_v(self) -> None:
        self.assertEqual(updater._version_tuple("v0.20.0"), (0, 20, 0))
        self.assertEqual(updater._version_tuple("0.20.0"), (0, 20, 0))

    def test_version_tuple_tolerates_prerelease_suffix(self) -> None:
        self.assertEqual(updater._version_tuple("0.20.0-rc1"), (0, 20, 0))

    def test_is_newer_true_when_later(self) -> None:
        self.assertTrue(updater.is_newer("0.20.1", current=(0, 20, 0)))
        self.assertTrue(updater.is_newer("v0.21.0", current=(0, 20, 0)))

    def test_is_newer_false_when_equal_or_older(self) -> None:
        self.assertFalse(updater.is_newer("0.20.0", current=(0, 20, 0)))
        self.assertFalse(updater.is_newer("0.19.9", current=(0, 20, 0)))


class ChannelDetectionTests(unittest.TestCase):
    def _with_pkg_file(self, path: str):
        import pickup

        return mock.patch.object(pickup, "__file__", os.path.join(path, "__init__.py"))

    def test_detects_brew_cellar_path(self) -> None:
        with self._with_pkg_file("/home/linuxbrew/.linuxbrew/Cellar/pickup/0.20.0/lib/python3.12/site-packages/pickup"):
            self.assertEqual(updater.detect_channel(), "brew")

    def test_detects_pip_user_site_packages(self) -> None:
        with mock.patch.object(updater.site, "getusersitepackages", return_value="/home/user/.local/lib/python3.12/site-packages"):
            with mock.patch.object(updater.site, "getsitepackages", return_value=[]):
                with self._with_pkg_file("/home/user/.local/lib/python3.12/site-packages/pickup"):
                    self.assertEqual(updater.detect_channel(), "pip")

    def test_detects_dev_source_checkout(self) -> None:
        with mock.patch.object(updater.site, "getusersitepackages", return_value="/home/user/.local/lib/python3.12/site-packages"):
            with mock.patch.object(updater.site, "getsitepackages", return_value=[]):
                with self._with_pkg_file("/Users/demo/Codes/pickup/cli/src/pickup"):
                    self.assertEqual(updater.detect_channel(), "dev")

    def test_find_checkout_root_and_stale_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "cli")
            src = os.path.join(root, "src", "pickup")
            os.makedirs(src)
            with open(os.path.join(root, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write('[project]\nname = "pickup"\nversion = "0.0.0"\n')
            with open(os.path.join(src, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write("__version__ = '0.0.0'\n")
            self.assertEqual(updater.find_checkout_root(src), root)
            with self._with_pkg_file(src):
                self.assertTrue(updater.is_loaded_from_checkout(root))
                self.assertIsNone(updater.stale_source_warning(cwd=root))
            with self._with_pkg_file(
                "/home/u/.local/pipx/venvs/pickup/lib/python3.12/site-packages/pickup"
            ):
                warn = updater.stale_source_warning(cwd=root)
                self.assertIsNotNone(warn)
                assert warn is not None
                self.assertIn("dev-install.sh", warn)
                self.assertIn(root, warn)

    def test_install_report_includes_paths(self) -> None:
        report = updater.install_report()
        self.assertIn("version", report)
        self.assertTrue(os.path.isabs(report["package_file"]))
        self.assertIn(report["channel"], ("brew", "pip", "dev"))
        self.assertIn("loaded_from_checkout", report)
        self.assertIn("stale_source_warning", report)

    def test_is_updatable_only_for_brew_and_pip(self) -> None:
        self.assertTrue(updater.is_updatable("brew"))
        self.assertTrue(updater.is_updatable("pip"))
        self.assertFalse(updater.is_updatable("dev"))

    def test_update_command_brew(self) -> None:
        self.assertEqual(updater.update_command("0.21.0", "brew"), ["brew", "upgrade", "pickup"])

    def test_update_command_pip_user_site(self) -> None:
        with mock.patch.object(updater.site, "getusersitepackages", return_value="/home/user/.local/lib/python3.12/site-packages"):
            with self._with_pkg_file("/home/user/.local/lib/python3.12/site-packages/pickup"):
                cmd = updater.update_command("0.21.0", "pip")
        self.assertIn("--user", cmd)
        self.assertIn("git+https://github.com/x0c/pickup.git@v0.21.0", cmd)

    def test_update_command_dev_returns_none(self) -> None:
        self.assertIsNone(updater.update_command("0.21.0", "dev"))


class FetchLatestTests(unittest.TestCase):
    def test_parses_tag_name_and_strips_v(self) -> None:
        payload = json.dumps({"tag_name": "v0.21.0"}).encode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        with mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp()):
            self.assertEqual(updater.fetch_latest(), "0.21.0")

    def test_returns_none_on_network_error(self) -> None:
        with mock.patch.object(
            updater.urllib.request, "urlopen",
            side_effect=updater.urllib.error.URLError("boom"),
        ):
            self.assertIsNone(updater.fetch_latest())

    def test_returns_none_on_malformed_json(self) -> None:
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not json"

        with mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp()):
            self.assertIsNone(updater.fetch_latest())


class DismissStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_file = os.path.join(self._tmpdir.name, "update.json")
        patcher = mock.patch.object(updater, "STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        dir_patcher = mock.patch.object(updater, "CACHE_DIR", self._tmpdir.name)
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)

    def test_should_prompt_false_when_not_newer(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            self.assertFalse(updater.should_prompt("0.20.0"))
            self.assertFalse(updater.should_prompt("0.19.0"))

    def test_should_prompt_true_for_newer_version_first_time(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            self.assertTrue(updater.should_prompt("0.21.0"))

    def test_dismiss_suppresses_same_version_same_day(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            self.assertFalse(updater.should_prompt("0.21.0"))

    def test_dismiss_does_not_suppress_next_day(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            with mock.patch.object(updater, "_today", return_value="2099-01-01"):
                self.assertTrue(updater.should_prompt("0.21.0"))

    def test_dismiss_does_not_suppress_a_newer_version(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            self.assertTrue(updater.should_prompt("0.22.0"))


class CliUpdateTests(unittest.TestCase):
    """`pickup update` 终端子命令：三条主路径（dev 无法自动升级 / 已是最新 /
    有新版本并成功升级），全部走 stdout 断言，不发真实网络请求。"""

    def _capture(self, **patches) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with mock.patch.multiple(updater, **patches), redirect_stdout(buf):
            code = updater.cli_update()
        return code, buf.getvalue()

    def test_dev_channel_prints_manual_hint_and_exits_nonzero(self) -> None:
        code, out = self._capture(detect_channel=lambda: "dev")
        self.assertEqual(code, 1)
        self.assertIn("x0c/pickup", out)

    def test_network_failure_prints_check_failed_and_exits_nonzero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: None,
        )
        self.assertEqual(code, 1)

    def test_already_latest_prints_message_and_exits_zero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "0.1.0",
            current_version=lambda: (9, 9, 9),
        )
        self.assertEqual(code, 0)
        self.assertIn("9.9.9", out)

    def test_newer_version_runs_update_and_reports_success(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "9.9.9",
            current_version=lambda: (0, 1, 0),
            run_update=lambda latest, channel=None: (True, "install ok"),
        )
        self.assertEqual(code, 0)
        self.assertIn("9.9.9", out)
        self.assertIn("install ok", out)

    def test_newer_version_update_failure_exits_nonzero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "9.9.9",
            current_version=lambda: (0, 1, 0),
            run_update=lambda latest, channel=None: (False, "boom"),
        )
        self.assertEqual(code, 1)
        self.assertIn("boom", out)


if __name__ == "__main__":
    unittest.main()
