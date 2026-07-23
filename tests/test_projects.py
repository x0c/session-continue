"""项目发现与快捷启动 `pickup <runtime> <project>` 分流。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pickup
from pickup import projects
from pickup.models import LaunchPlan


def _touch_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)


class GitScanTests(unittest.TestCase):
    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    def test_scan_finds_git_roots_and_skips_nested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _touch_git(root / "subswap")
            _touch_git(root / "a" / "b" / "LingoWeave")
            _touch_git(root / "subswap" / "vendor" / "nested")  # 应被剪枝

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(
                set(found),
                {str(root / "subswap"), str(root / "a" / "b" / "LingoWeave")},
            )

    def test_scan_skips_stversions_syncthing_snapshots(self) -> None:
        """回归：Syncthing `.stversions` 里的 `.git` 不得当成项目。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "Codes" / "subswap"
            snap = root / "Codes" / ".stversions" / "subswap"
            _touch_git(real)
            _touch_git(snap)

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(real)])

    def test_scan_skips_dot_dirs_and_node_modules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _touch_git(root / "ok")
            _touch_git(root / "node_modules" / "pkg")
            _touch_git(root / ".cache" / "hidden")

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(root / "ok")])

    def test_scan_resolves_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real"
            link = root / "link"
            _touch_git(real / "pickup")
            link.symlink_to(real)

            found = projects.scan_git_roots([str(link)], depth=4, use_cache=False)
            self.assertEqual(found, [str((real / "pickup").resolve())])

    def test_pickup_project_roots_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _touch_git(root / "only")
            with mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": str(root)}):
                projects.clear_filesystem_cache()
                found = projects.scan_git_roots(depth=2, use_cache=False)
            self.assertEqual(found, [str(root / "only")])

    def test_empty_pickup_project_roots_skips_filesystem(self) -> None:
        with mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": ""}):
            self.assertEqual(projects.configured_roots(), [])
            self.assertEqual(projects.scan_git_roots(use_cache=False), [])


class MatchResolveTests(unittest.TestCase):
    def _projects(self, *paths: str) -> list[projects.Project]:
        return projects.discover(paths, scan_filesystem=False)

    def test_fuzzy_case_insensitive_unique(self) -> None:
        items = self._projects("/Codes/SubSwap", "/Codes/pickup")
        matched = projects.match_projects("subswap", items)
        self.assertEqual([p.path for p in matched], ["/Codes/SubSwap"])
        self.assertEqual(projects.resolve_query("SUB", items), "/Codes/SubSwap")

    def test_zero_matches_raises(self) -> None:
        items = self._projects("/Codes/pickup")
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("nope", items)
        self.assertIn("未找到", str(ctx.exception))

    def test_multiple_matches_interactive_pick(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        stdin = io.StringIO("2\n")
        stdout = io.StringIO()
        cwd = projects.resolve_query(
            "cli", items, stdin=stdin, stdout=stdout, interactive=True,
        )
        self.assertEqual(cwd, "/b/cli")
        self.assertIn("多个项目匹配", stdout.getvalue())

    def test_multiple_matches_noninteractive_raises(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("cli", items, interactive=False)
        self.assertIn("多个项目", str(ctx.exception))


class ProjectEntriesTests(unittest.TestCase):
    def test_merges_git_and_session_stats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _touch_git(root / "diskonly")
            sessions = {
                "claude": [
                    {"cwd": str(root / "diskonly"), "mtime": 9},
                    {"cwd": str(root / "sessiononly"), "mtime": 5},
                ],
            }
            entries = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            by_key = {e["cwd_key"]: e for e in entries}
            self.assertEqual(by_key[str(root / "diskonly")]["count"], 1)
            self.assertEqual(by_key[str(root / "sessiononly")]["count"], 1)
            # 纯 git、无会话的项目 count=0（本例 diskonly 有会话；再造一个）
            _touch_git(root / "puregit")
            entries2 = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            pure = next(e for e in entries2 if e["cwd_key"] == str(root / "puregit"))
            self.assertEqual(pure["count"], 0)


class DirectLaunchProjectTests(unittest.TestCase):
    """`pickup claude <project>` 与透传分流。"""

    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    def test_passthrough_when_first_arg_is_flag(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        registry = mock.Mock()
        registry.build_passthrough_plan.return_value = plan
        registry.ids = ("claude",)

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            pickup._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        execute_launch.assert_called_once_with(plan)

    def test_project_mode_builds_new_session_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "subswap"
            _touch_git(proj)
            new_plan = LaunchPlan(("claude", "--dangerously-skip-permissions"), str(proj))

            runtime = mock.Mock()
            runtime.build_new_session_plan.return_value = new_plan
            registry = mock.Mock()
            registry.get.return_value = runtime
            registry.scan_all.return_value = {"claude": []}
            registry.ids = ("claude",)

            with (
                mock.patch.dict(os.environ, {"PICKUP_PROJECT_ROOTS": str(root)}),
                mock.patch.object(pickup, "keepalive") as keepalive_mock,
                mock.patch.object(pickup, "execute_launch") as execute_launch,
                mock.patch.object(pickup, "_require_tmux"),
                mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
                mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
            ):
                projects.clear_filesystem_cache()
                keepalive_mock.enabled.return_value = False
                keepalive_mock.new_session_ident.return_value = "xxxx"
                pickup._dispatch_direct_launch(["claude", "subswap"], registry)

            registry.get.assert_called_with("claude")
            runtime.build_new_session_plan.assert_called_once_with(str(proj))
            registry.build_passthrough_plan.assert_not_called()
            execute_launch.assert_called_once_with(new_plan)

    def test_project_mode_rejects_extra_args(self) -> None:
        registry = mock.Mock()
        with (
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys, "stderr", new_callable=io.StringIO) as err,
        ):
            with self.assertRaises(SystemExit) as ctx:
                pickup._dispatch_direct_launch(["claude", "subswap", "extra"], registry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("不接受额外参数", err.getvalue())
        registry.build_passthrough_plan.assert_not_called()

    def test_no_keepalive_passthrough_with_flag_args(self) -> None:
        plan = LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox", "--resume", "x"), None)
        registry = mock.Mock()
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            pickup._dispatch_direct_launch(
                ["--no-keepalive", "codex", "--resume", "x"], registry,
            )

        registry.build_passthrough_plan.assert_called_once_with("codex", ["--resume", "x"])
        execute_launch.assert_called_once_with(plan)


class LegacyDirectLaunchTestsUpdate(unittest.TestCase):
    """原 DirectLaunchTests 中依赖「裸位置参数透传」的用例改为 flag 透传。"""

    def test_passes_through_flag_args_and_wraps_with_keepalive(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        wrapped = LaunchPlan(("tmux", "-L", "pickup-keepalive", "new-session"), None)
        registry = mock.Mock()
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
            mock.patch.object(pickup, "_require_tmux"),
            mock.patch.object(pickup.sys.stdin, "isatty", return_value=False),
            mock.patch.object(pickup.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"
            keepalive_mock.wrap_plan.return_value = wrapped
            pickup._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        keepalive_mock.wrap_plan.assert_called_once_with(plan, "claude", "xxxx")
        execute_launch.assert_called_once_with(wrapped)


if __name__ == "__main__":
    unittest.main()
