from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from pickup import keepalive
from pickup.models import LaunchPlan


class EnabledTests(unittest.TestCase):
    def test_disabled_flag_wins(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled(disabled_flag=True))

    def test_env_var_disables(self) -> None:
        with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE": "0"}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_legacy_env_var_still_disables(self) -> None:
        # 改名前的旧变量名 SC_KEEPALIVE 继续生效，不悄悄破坏已有的用户配置
        with mock.patch.dict("os.environ", {"SC_KEEPALIVE": "0"}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_already_inside_tmux_skips(self) -> None:
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,1234,0"}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_already_inside_screen_skips(self) -> None:
        with mock.patch.dict("os.environ", {"STY": "1234.pts-0.host"}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_missing_tmux_binary_skips(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value=None):
            self.assertFalse(keepalive.enabled())

    def test_default_enabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertTrue(keepalive.enabled())


class WrapPlanTests(unittest.TestCase):
    def test_wraps_argv_with_dedicated_socket_and_config(self) -> None:
        plan = LaunchPlan(("claude", "--resume", "abcd1234efgh"), "/work/dir")

        wrapped = keepalive.wrap_plan(plan, "claude", "abcd1234efgh")

        self.assertEqual(wrapped.argv[0], "tmux")
        self.assertIn("-L", wrapped.argv)
        self.assertEqual(wrapped.argv[wrapped.argv.index("-L") + 1], keepalive.SOCKET_NAME)
        self.assertIn("new-session", wrapped.argv)
        self.assertIn("-A", wrapped.argv)
        self.assertIn("-s", wrapped.argv)
        session_name = wrapped.argv[wrapped.argv.index("-s") + 1]
        self.assertTrue(session_name.startswith("pickup-claude-"))
        self.assertIn("-c", wrapped.argv)
        self.assertEqual(wrapped.argv[wrapped.argv.index("-c") + 1], "/work/dir")
        self.assertIn("--", wrapped.argv)
        # 原始 argv 完整保留在 -- 之后，不能被拆散或改写
        tail = wrapped.argv[wrapped.argv.index("--") + 1:]
        self.assertEqual(tail, plan.argv)
        # tmux 自己接管 -c，不需要 execute_launch 再 os.chdir 一次
        self.assertIsNone(wrapped.cwd)

    def test_injects_both_new_and_legacy_env_vars(self) -> None:
        plan = LaunchPlan(("claude",), None)

        wrapped = keepalive.wrap_plan(plan, "claude", "abcd1234")

        for name in ("PICKUP_RUNTIME", "PICKUP_SESSION_ID", "SC_RUNTIME", "SC_SESSION_ID"):
            self.assertTrue(any(arg == f"{name}={'claude' if name.endswith('RUNTIME') else 'abcd1234'}"
                                for arg in wrapped.argv), name)

    def test_wrap_plan_without_cwd_skips_dash_c(self) -> None:
        plan = LaunchPlan(("codex",), None)

        wrapped = keepalive.wrap_plan(plan, "codex", "session-id")

        self.assertNotIn("-c", wrapped.argv)

    def test_session_name_truncates_long_ident(self) -> None:
        plan = LaunchPlan(("claude",), None)

        wrapped = keepalive.wrap_plan(plan, "claude", "0123456789abcdef")

        session_name = wrapped.argv[wrapped.argv.index("-s") + 1]
        self.assertEqual(session_name, "pickup-claude-01234567")


class AttachPlanTests(unittest.TestCase):
    def test_returns_none_without_keepalive_name(self) -> None:
        self.assertIsNone(keepalive.attach_plan({"id": "x"}))

    def test_builds_attach_command(self) -> None:
        plan = keepalive.attach_plan({"keepalive_name": "sc-claude-abcd1234"})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.argv[0], "tmux")
        self.assertIn("attach-session", plan.argv)
        self.assertIn("-t", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("-t") + 1], "sc-claude-abcd1234")
        self.assertIsNone(plan.cwd)


def _fake_check_output(list_sessions_output: str, ps_output: str):
    def _run(argv, **kwargs):
        if argv[:1] == ["ps"]:
            return ps_output.encode()
        if "list-sessions" in argv:
            return list_sessions_output.encode()
        raise AssertionError(f"unexpected subprocess call: {argv}")
    return _run


class AnnotateTests(unittest.TestCase):
    def test_matches_session_by_pid_ancestor_chain(self) -> None:
        # tmux pane 顶层 pid 是 100（tmux 直接 exec 的进程），但运行时自己注册的
        # "活跃 pid" 是 102——中间隔了一层 fork，必须靠祖先链才能追上，不能只比对 pid 相等。
        sessions = [{"id": "s1", "pid": 102}, {"id": "s2", "pid": 999}]
        list_sessions_out = "pickup-claude-abcd1234|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n  102   101\n  999     1\n"

        with mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "pickup-claude-abcd1234")
        self.assertNotIn("keepalive_name", sessions[1])

    def test_legacy_sc_prefix_sessions_still_matched(self) -> None:
        # 改名前创建的 sc-* 保活会话（可能仍在用户机器上跑）必须继续被识别/回收
        sessions = [{"id": "s1", "pid": 101}]
        list_sessions_out = "sc-claude-abcd1234|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n"

        with mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "sc-claude-abcd1234")

    def test_no_tmux_server_running_is_a_noop(self) -> None:
        sessions = [{"id": "s1", "pid": 100}]

        with mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output",
                         side_effect=subprocess.CalledProcessError(1, "tmux")):
            keepalive.annotate(sessions)

        self.assertNotIn("keepalive_name", sessions[0])

    def test_sessions_without_pid_are_skipped(self) -> None:
        sessions = [{"id": "s1", "pid": None}, {"id": "s2"}]

        with mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output") as mocked:
            keepalive.annotate(sessions)

        mocked.assert_not_called()


class ReapIdleTests(unittest.TestCase):
    def test_kills_sessions_past_idle_threshold(self) -> None:
        # 新旧两种前缀的会话都要被回收（sc-* 是改名前留下的存量）
        rows = "pickup-claude-old|1000\nsc-claude-legacy|1000\npickup-claude-fresh|99999\n"
        now = 100000.0  # 前两个空闲 99000 秒 ≈ 27.5 小时，超过默认 6 小时阈值

        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output", return_value=rows.encode()), \
             mock.patch("pickup.keepalive.kill", return_value=True) as mocked_kill:
            reaped = keepalive.reap_idle(now=now)

        self.assertEqual(reaped, ["pickup-claude-old", "sc-claude-legacy"])
        self.assertEqual(mocked_kill.call_count, 2)

    def test_zero_threshold_disables_reaping(self) -> None:
        with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE_IDLE_HOURS": "0"}, clear=True), \
             mock.patch("pickup.keepalive.subprocess.check_output") as mocked:
            reaped = keepalive.reap_idle(now=100000.0)

        self.assertEqual(reaped, [])
        mocked.assert_not_called()

    def test_legacy_env_threshold_still_honored(self) -> None:
        with mock.patch.dict("os.environ", {"SC_KEEPALIVE_IDLE_HOURS": "0"}, clear=True), \
             mock.patch("pickup.keepalive.subprocess.check_output") as mocked:
            reaped = keepalive.reap_idle(now=100000.0)

        self.assertEqual(reaped, [])
        mocked.assert_not_called()

    def test_custom_threshold_from_env(self) -> None:
        rows = "pickup-claude-borderline|1000\n"
        now = 1000.0 + 3600.0  # 恰好空闲 1 小时

        with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE_IDLE_HOURS": "0.5"}, clear=True), \
             mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.check_output", return_value=rows.encode()), \
             mock.patch("pickup.keepalive.kill", return_value=True) as mocked_kill:
            reaped = keepalive.reap_idle(now=now)

        self.assertEqual(reaped, ["pickup-claude-borderline"])
        mocked_kill.assert_called_once()


class KillTests(unittest.TestCase):
    def test_kill_invokes_tmux_kill_session(self) -> None:
        with mock.patch("pickup.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("pickup.keepalive.subprocess.run") as mocked_run:
            result = keepalive.kill("sc-claude-abcd1234")

        self.assertTrue(result)
        argv = mocked_run.call_args[0][0]
        self.assertIn("kill-session", argv)
        self.assertIn("-t", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "sc-claude-abcd1234")

    def test_kill_returns_false_without_tmux(self) -> None:
        with mock.patch("pickup.keepalive.shutil.which", return_value=None):
            self.assertFalse(keepalive.kill("sc-claude-abcd1234"))


if __name__ == "__main__":
    unittest.main()
