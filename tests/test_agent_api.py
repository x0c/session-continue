from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pickup import agent_api
from pickup import titles
from pickup.models import ConversationMessage, Handoff, LaunchPlan
from pickup.runtime.claude import ClaudeRuntime
from pickup.runtime.codex import CodexRuntime
from pickup.runtime import RuntimeRegistry
from pickup.runtime.base import BaseRuntime, LaunchError


class FakeRuntime(BaseRuntime):
    id = "fake"
    display_name = "Fake"
    executable = "true"  # 系统自带的 no-op 命令，is_available() 恒真，不需要真的装运行时
    history_reading_hint = "测试格式提示"

    def __init__(self, sessions: list[dict], conversations: dict[str, list[ConversationMessage]]):
        self._sessions = sessions
        self._conversations = conversations

    def scan_sessions(self, limit: int) -> list[dict]:
        return self._sessions[:limit]

    def load_conversation(self, session: dict) -> list[ConversationMessage]:
        return self._conversations.get(session["id"], [])

    def build_resume_plan(self, session: dict) -> LaunchPlan:
        return LaunchPlan((self.executable, "resume", str(session["id"])), None)

    def build_continue_plan(self, session: dict, instruction: str) -> LaunchPlan:
        return LaunchPlan((self.executable, "resume", str(session["id"]), instruction), None)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan((self.executable, handoff.render_prompt()), None)

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable,), cwd)


class BrokenRuntime(BaseRuntime):
    """scan_sessions 必抛异常的假运行时，供验证 _scan_runtimes 的异常隔离。"""

    id = "broken"
    display_name = "Broken"
    executable = "true"
    history_reading_hint = "测试格式"

    def scan_sessions(self, limit: int) -> list[dict]:
        raise RuntimeError("模拟某条真实会话记录触发的未预料解析异常")

    def load_conversation(self, session: dict) -> list:
        return []

    def build_resume_plan(self, session: dict) -> LaunchPlan:
        return LaunchPlan((self.executable,), None)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan((self.executable,), None)

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable,), cwd)


def _session(history_path: str, **overrides) -> dict:
    base = {
        "source": "fake",
        "id": "aaaa1111-0000-0000-0000-000000000000",
        "short_id": "aaaa1111",
        "cwd": "/tmp/weather-app",
        "cwd_display": "/tmp/weather-app",
        "mtime": 1700000000.0,
        "display_time": "2023-11-14 22:13",
        "size_bytes": 100,
        "size_kb": 0.1,
        "native_title": None,
        # 三个候选与 fallback_title 保持一致长度关系，避免 titles._temporary_title 的
        # "取最短候选" 策略在测试之间选出不同文本，让标题解析结果不稳定。
        "fallback_title": "天气 App 开发",
        "status_tag": titles.STATUS_DONE,
        "first_user_msg": "天气 App 开发",
        "last_user_msg": "天气 App 开发",
        "last_agent_msg": "天气 App 开发",
        "path": history_path,
    }
    base.update(overrides)
    return base


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.history_path = str(Path(self._tmpdir.name) / "aaaa1111.jsonl")
        Path(self.history_path).write_text("{}\n", encoding="utf-8")

        self.session = _session(self.history_path)
        self.messages = [
            ConversationMessage("user", "帮我做一个天气 App"),
            ConversationMessage("assistant", "好的，先搭个骨架"),
            ConversationMessage("user", "再加个下雨提醒"),
            ConversationMessage("assistant", "已经加好下雨提醒了"),
        ]
        self.runtime = FakeRuntime([self.session], {self.session["id"]: self.messages})
        self.registry = RuntimeRegistry((self.runtime,))

        cache_patcher = mock.patch.object(titles, "load_cache", return_value={})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    # ---- session_payload / status mapping ----

    def test_session_payload_maps_status_to_english_enum(self) -> None:
        payload = agent_api.session_payload(self.session, {})
        self.assertEqual(payload["status"], "done")

    def test_session_payload_fields_filter(self) -> None:
        payload = agent_api.session_payload(self.session, {}, fields=["id", "status"])
        self.assertEqual(set(payload), {"id", "status"})

    def test_session_payload_prefers_cached_generated_title_over_fallback(self) -> None:
        cache = {"fake:aaaa1111-0000-0000-0000-000000000000": {
            "fp": f"v{titles.TITLE_CACHE_VERSION}:100", "title": "天气 App 完整实现",
        }}
        payload = agent_api.session_payload(self.session, cache)
        self.assertEqual(payload["title"], "天气 App 完整实现")

    # ---- list ----

    def test_cmd_list_filters_by_status_and_cwd(self) -> None:
        args = argparse_namespace(runtime=None, limit=50, status="done", cwd="weather", fields=None)
        result = agent_api.cmd_list(args, self.registry)
        self.assertEqual(result["data"]["count"], 1)

        args = argparse_namespace(runtime=None, limit=50, status="pending", cwd=None, fields=None)
        result = agent_api.cmd_list(args, self.registry)
        self.assertEqual(result["data"]["count"], 0)

    def test_cmd_list_isolates_exception_from_one_runtime(self) -> None:
        # 单个运行时的扫描异常（如某条真实会话记录触发未预料的解析 bug）不能
        # 拖垮其余运行时的结果，也不能让 list 命令直接报错退出。
        registry = RuntimeRegistry((self.runtime, BrokenRuntime()))
        args = argparse_namespace(runtime=None, limit=50, status=None, cwd=None, fields=None)

        result = agent_api.cmd_list(args, registry)

        self.assertEqual(result["data"]["count"], 1)

    # ---- search ----

    def test_cmd_search_quick_match_on_title(self) -> None:
        args = argparse_namespace(keywords=["天气"], deep=False, runtime=None, limit=50)
        result = agent_api.cmd_search(args, self.registry)
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["sessions"][0]["matched_via"], "quick")

    def test_cmd_search_deep_match_falls_back_to_conversation(self) -> None:
        args = argparse_namespace(keywords=["骨架"], deep=False, runtime=None, limit=50)
        result = agent_api.cmd_search(args, self.registry)
        self.assertEqual(result["data"]["count"], 0)

        args = argparse_namespace(keywords=["骨架"], deep=True, runtime=None, limit=50)
        result = agent_api.cmd_search(args, self.registry)
        self.assertEqual(result["data"]["count"], 1)
        session = result["data"]["sessions"][0]
        self.assertEqual(session["matched_via"], "deep")
        self.assertIn("骨架", session["snippet"])

    def test_cmd_search_multiple_keywords_are_and(self) -> None:
        args = argparse_namespace(keywords=["天气", "不存在的词"], deep=False, runtime=None, limit=50)
        result = agent_api.cmd_search(args, self.registry)
        self.assertEqual(result["data"]["count"], 0)

    # ---- show ----

    def test_cmd_show_default_tail_and_full(self) -> None:
        args = argparse_namespace(session="aaaa1111", messages=None, full=False, limit=200)
        result = agent_api.cmd_show(args, self.registry)
        self.assertEqual(result["data"]["message_count_shown"], 4)  # 只有 4 条消息，未超过默认 20

        args = argparse_namespace(session="aaaa1111", messages=2, full=False, limit=200)
        result = agent_api.cmd_show(args, self.registry)
        self.assertEqual(result["data"]["message_count_shown"], 2)
        self.assertEqual(result["data"]["messages"][-1]["text"], "已经加好下雨提醒了")

        args = argparse_namespace(session="aaaa1111", messages=1, full=True, limit=200)
        result = agent_api.cmd_show(args, self.registry)
        self.assertEqual(result["data"]["message_count_shown"], 4)  # --full 忽略 --messages

    def test_cmd_show_messages_expose_readonly_time_fields(self) -> None:
        session = _session(self.history_path, id="bbbb2222-0000-0000-0000-000000000000", short_id="bbbb2222")
        messages = [
            ConversationMessage("user", "有时间戳的消息", 1_700_000_000.0),
            ConversationMessage("assistant", "老格式没有时间戳的消息"),
        ]
        runtime = FakeRuntime([session], {session["id"]: messages})
        registry = RuntimeRegistry((runtime,))

        args = argparse_namespace(session="bbbb2222", messages=None, full=False, limit=200)
        result = agent_api.cmd_show(args, registry)

        first, second = result["data"]["messages"]
        self.assertEqual(first["time"], agent_api.format_message_time(1_700_000_000.0))
        self.assertEqual(first["mtime"], 1_700_000_000.0)
        self.assertIsNone(second["time"])
        self.assertIsNone(second["mtime"])

    def test_cmd_show_fields_overrides_compact_default(self) -> None:
        # oc agents（OpenConductor 控制面）依赖 --fields 显式取回 cwd/pid：
        # --compact 单独使用时默认字段集（DEFAULT_SHOW_FIELDS）不含这两个字段，
        # 曾导致停止/续接判断拿到空 cwd 和空 pid。
        args = argparse_namespace(session="aaaa1111", messages=None, full=False, limit=200,
                                   out=None, compact=True, fields="id,cwd,pid,live")
        result = agent_api.cmd_show(args, self.registry)
        self.assertEqual(set(result["data"]), {"id", "cwd", "pid", "live"})
        self.assertEqual(result["data"]["cwd"], "/tmp/weather-app")

    # ---- context ----

    def test_cmd_context_returns_handoff_and_resume_command(self) -> None:
        args = argparse_namespace(session="aaaa1111", limit=200)
        result = agent_api.cmd_context(args, self.registry)
        data = result["data"]
        self.assertEqual(data["runtime"], "fake")
        self.assertEqual(data["history_path"], self.history_path)
        self.assertIn("天气 App 开发", data["suggested_prompt"])
        self.assertEqual(data["resume_command"], f'true resume aaaa1111-0000-0000-0000-000000000000')
        self.assertFalse(data["cwd_exists"])  # /tmp/weather-app 在测试机上并不存在

    def test_cmd_context_missing_history_file_raises_api_error(self) -> None:
        missing = _session("/no/such/file.jsonl")
        runtime = FakeRuntime([missing], {})
        registry = RuntimeRegistry((runtime,))
        args = argparse_namespace(session="aaaa1111", limit=200)
        with self.assertRaises(agent_api.ApiError) as cm:
            agent_api.cmd_context(args, registry)
        self.assertEqual(cm.exception.code, "history_unavailable")

    # ---- plan continue ----

    def test_cmd_plan_continue_returns_structured_safe_argv(self) -> None:
        instruction = '继续完成任务；不要解释 $HOME 或 "引号"'
        args = argparse_namespace(session="fake:aaaa1111", instruction=instruction, limit=200)
        result = agent_api.cmd_plan_continue(args, self.registry)
        data = result["data"]

        self.assertEqual(data["session_ref"], f"fake:{self.session['id']}")
        self.assertEqual(data["runtime"], "fake")
        self.assertEqual(data["cwd"], "/tmp/weather-app")
        self.assertEqual(data["capabilities"]["execution"], "external_only")
        self.assertIsInstance(data["launch"]["argv"], list)
        self.assertEqual(data["launch"]["argv"][-1], instruction)
        self.assertNotIsInstance(data["launch"]["argv"], str)

    def test_cmd_plan_continue_has_no_execution_side_effect(self) -> None:
        args = argparse_namespace(session="aaaa1111", instruction="继续处理", limit=200)
        with mock.patch("os.execvp") as execvp, mock.patch("os.chdir") as chdir:
            result = agent_api.cmd_plan_continue(args, self.registry)
        self.assertTrue(result["ok"])
        execvp.assert_not_called()
        chdir.assert_not_called()

    def test_cmd_plan_continue_rejects_unresumable_session(self) -> None:
        with mock.patch.object(self.runtime, "build_continue_plan", side_effect=LaunchError("运行时不支持")):
            args = argparse_namespace(session="aaaa1111", instruction="继续处理", limit=200)
            with self.assertRaises(agent_api.ApiError) as cm:
                agent_api.cmd_plan_continue(args, self.registry)
        self.assertEqual(cm.exception.code, "not_resumable")
        self.assertEqual(cm.exception.exit_code, agent_api.EXIT_ERROR)

    def test_cmd_plan_continue_rejects_blank_instruction(self) -> None:
        args = argparse_namespace(session="aaaa1111", instruction="  ", limit=200)
        with self.assertRaises(agent_api.ApiError) as cm:
            agent_api.cmd_plan_continue(args, self.registry)
        self.assertEqual(cm.exception.code, "usage_error")

    # ---- resolve_ref ----

    def test_resolve_ref_by_prefix(self) -> None:
        session = agent_api.resolve_ref(self.registry, "aaaa1111", 50)
        self.assertEqual(session["id"], self.session["id"])

    def test_resolve_ref_by_runtime_qualified_id(self) -> None:
        session = agent_api.resolve_ref(self.registry, "fake:aaaa1111", 50)
        self.assertEqual(session["id"], self.session["id"])

    def test_resolve_ref_not_found(self) -> None:
        with self.assertRaises(agent_api.ApiError) as cm:
            agent_api.resolve_ref(self.registry, "zzzz-not-there", 50)
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(cm.exception.exit_code, agent_api.EXIT_NOT_FOUND)

    def test_resolve_ref_ambiguous_prefix(self) -> None:
        twin = _session(self.history_path, id="aaaa2222-0000-0000-0000-000000000000", short_id="aaaa2222")
        runtime = FakeRuntime([self.session, twin], {})
        registry = RuntimeRegistry((runtime,))

        with self.assertRaises(agent_api.ApiError) as cm:
            agent_api.resolve_ref(registry, "aaaa", 50)
        self.assertEqual(cm.exception.code, "ambiguous")
        self.assertEqual(cm.exception.exit_code, agent_api.EXIT_AMBIGUOUS)
        self.assertTrue(cm.exception.next_commands)

    # ---- describe ----

    def test_describe_lists_all_commands_without_target(self) -> None:
        args = argparse_namespace(target=None)
        result = agent_api.cmd_describe(args, self.registry)
        names = {c["name"] for c in result["data"]["commands"]}
        self.assertEqual(names, set(agent_api.COMMAND_NAMES))

    def test_describe_unknown_command_raises_not_found(self) -> None:
        args = argparse_namespace(target="bogus")
        with self.assertRaises(agent_api.ApiError) as cm:
            agent_api.cmd_describe(args, self.registry)
        self.assertEqual(cm.exception.exit_code, agent_api.EXIT_NOT_FOUND)

    def test_describe_plan_continue_uses_commands_spec(self) -> None:
        args = argparse_namespace(target=["plan", "continue"])
        result = agent_api.cmd_describe(args, self.registry)
        self.assertEqual(result["data"]["name"], "plan continue")
        self.assertEqual(
            result["data"]["help"],
            next(spec["help"] for spec in agent_api.COMMANDS if spec["name"] == "plan continue"),
        )
        flags = [arg["flags"] for arg in result["data"]["args"]]
        self.assertIn(["--instruction"], flags)

    def test_commands_spec_and_handlers_are_in_sync(self) -> None:
        self.assertEqual(set(agent_api.COMMAND_NAMES), set(agent_api.HANDLERS))

    def test_diagnose_is_readonly_and_reports_paths(self) -> None:
        args = argparse_namespace()
        result = agent_api.cmd_diagnose(args, self.registry)
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertIn("cache_dir", data)
        self.assertIn("events_log", data)
        self.assertIn("embed_error_log", data)
        self.assertIn("last_error", data)
        self.assertEqual(data["runtime_label_style_claude"], "bold #D97757")
        self.assertIsInstance(data["hints"], list)
        self.assertIn("package_file", data)
        self.assertIn("install_channel", data)
        self.assertIn("stale_source_warning", data)
        self.assertIn("version", data)

    def test_diagnose_includes_last_error_from_embed_log(self) -> None:
        from pickup import observe

        with tempfile.TemporaryDirectory() as cache:
            events = os.path.join(cache, "events.log")
            embed_err = os.path.join(cache, "embed-error.log")
            with mock.patch.object(observe, "CACHE_DIR", cache), mock.patch.object(
                observe, "EVENTS_LOG", events
            ), mock.patch.object(observe, "EMBED_ERROR_LOG", embed_err):
                observe.reset_for_tests()
                observe.init(debug=False)
                try:
                    raise NameError("name '_project_groups' is not defined")
                except NameError as exc:
                    observe.log_exception("TUI 未捕获异常", exc)
                result = agent_api.cmd_diagnose(argparse_namespace(), self.registry)
        self.assertTrue(result["ok"])
        last = result["data"]["last_error"]
        self.assertIsNotNone(last)
        self.assertEqual(last["where"], "TUI 未捕获异常")
        self.assertEqual(last["exc_type"], "NameError")
        self.assertIn("_project_groups", last["traceback"])

    # ---- dispatch: envelope + exit codes end-to-end ----

    def test_dispatch_ok_envelope_and_exit_code(self) -> None:
        with mock.patch.object(agent_api, "default_registry", return_value=self.registry):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = agent_api.dispatch(["list"])
        self.assertEqual(exit_code, agent_api.EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])

    def test_dispatch_missing_subcommand_is_usage_error(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as cm:
                agent_api.dispatch([])
        self.assertEqual(cm.exception.code, agent_api.EXIT_USAGE)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_dispatch_not_found_session_exit_code(self) -> None:
        with mock.patch.object(agent_api, "default_registry", return_value=self.registry):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = agent_api.dispatch(["show", "zzzz-not-there"])
        self.assertEqual(exit_code, agent_api.EXIT_NOT_FOUND)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_dispatch_invalid_choice_is_usage_error(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as cm:
                agent_api.dispatch(["list", "--status", "bogus"])
        self.assertEqual(cm.exception.code, agent_api.EXIT_USAGE)

    def test_dispatch_plan_continue_returns_envelope(self) -> None:
        with mock.patch.object(agent_api, "default_registry", return_value=self.registry):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = agent_api.dispatch([
                    "plan", "continue", "fake:aaaa1111", "--instruction", "继续完成天气 App",
                ])
        self.assertEqual(exit_code, agent_api.EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["data"]["launch"]["argv"], list)

    def test_dispatch_unknown_plan_session_returns_not_found(self) -> None:
        with mock.patch.object(agent_api, "default_registry", return_value=self.registry):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = agent_api.dispatch([
                    "plan", "continue", "fake:missing", "--instruction", "继续",
                ])
        self.assertEqual(exit_code, agent_api.EXIT_NOT_FOUND)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "not_found")


class RuntimeContinuationPlanTests(unittest.TestCase):
    """验证内置运行时都能构造带指令的非交互续接计划。"""

    def test_claude_plan_is_non_interactive_and_keeps_instruction_as_one_arg(self) -> None:
        instruction = '继续处理 $HOME；保留 "原样"'
        plan = ClaudeRuntime().build_continue_plan({"id": "claude-id", "cwd": "/no/such/cwd"}, instruction)
        self.assertEqual(plan.argv[0], "claude")
        self.assertIn("--resume", plan.argv)
        self.assertIn("--print", plan.argv)
        self.assertEqual(plan.argv[-1], instruction)
        self.assertIsNone(plan.cwd)

    def test_codex_plan_is_non_interactive_and_keeps_instruction_as_one_arg(self) -> None:
        instruction = '继续处理 $HOME；保留 "原样"'
        plan = CodexRuntime().build_continue_plan({"id": "codex-id", "cwd": "/no/such/cwd"}, instruction)
        self.assertEqual(plan.argv[:3], ("codex", "exec", "resume"))
        self.assertEqual(plan.argv[-1], instruction)
        self.assertIsNone(plan.cwd)


class SubprocessIntegrationTests(unittest.TestCase):
    """通过真实子进程验证 `python -m pickup` 的旧接口回归和非 TTY 自动降级；较慢，单独分组。"""

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pickup", *args],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_legacy_json_flag_still_produces_flat_array(self) -> None:
        proc = self._run(["--json", "--limit", "1"])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)

    def test_no_subcommand_without_tty_falls_back_to_json(self) -> None:
        # capture_output=True 使子进程的 stdin/stdout 都不是真实终端，应自动退化为 JSON 而不是崩溃。
        proc = self._run(["--limit", "1"])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)

    def test_agent_subcommand_reports_usage_error_exit_code(self) -> None:
        proc = self._run(["show"])
        self.assertEqual(proc.returncode, agent_api.EXIT_USAGE)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "usage_error")


def argparse_namespace(**kwargs):
    return _Namespace(**kwargs)


class _Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


if __name__ == "__main__":
    unittest.main()
