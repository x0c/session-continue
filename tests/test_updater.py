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
                with self._with_pkg_file("/Users/geraltgraham/Codes/pickup/cli/src/pickup"):
                    self.assertEqual(updater.detect_channel(), "dev")

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


if __name__ == "__main__":
    unittest.main()
