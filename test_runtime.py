from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import titles
from models import Handoff, LaunchPlan, LaunchRequest, NewSessionRequest, session_key
from runtime import BaseRuntime, LaunchError, RuntimeRegistry, default_registry


def _make_minimal_opencode_db(path: Path, session_id: str, title: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE session (id text PRIMARY KEY, project_id text, parent_id text, "
            "slug text, directory text, title text, version text, "
            "time_created integer, time_updated integer, time_archived integer)"
        )
        conn.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text, "
                      "time_created integer, time_updated integer, data text)")
        conn.execute("CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, "
                      "time_created integer, time_updated integer, data text)")
        conn.execute(
            "INSERT INTO session VALUES (?,'global',NULL,'x','/tmp',?, '1.0.0', 0, 0, NULL)",
            (session_id, title),
        )
        conn.commit()
    finally:
        conn.close()


class FakeRuntime(BaseRuntime):
    id = "gemini"
    display_name = "Gemini"
    executable = "gemini"
    history_reading_hint = "测试格式"

    def scan_sessions(self, limit: int) -> list[dict]:
        return []

    def load_conversation(self, session: dict) -> list:
        return []

    def build_resume_plan(self, session: dict) -> LaunchPlan:
        return LaunchPlan((self.executable, "--resume", str(session["id"])), None)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan((self.executable, handoff.render_prompt()), None)

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable,), cwd)


class RuntimeTests(unittest.TestCase):
    def _session(self, source: str, history_path: str, cwd: str) -> dict:
        return {
            "source": source,
            "id": "session-123",
            "path": history_path,
            "cwd": cwd,
            "fallback_title": "修复会话接力",
        }

    def test_native_resume_keeps_runtime_specific_command(self) -> None:
        registry = default_registry()
        session = self._session("claude", "/tmp/not-needed.jsonl", "/tmp/not-exists")

        plan = registry.build_launch_plan(LaunchRequest(session, "claude", "修复会话接力"))

        self.assertEqual(
            plan.argv,
            ("claude", "--dangerously-skip-permissions", "--resume", "session-123"),
        )
        self.assertIsNone(plan.cwd)

    def test_claude_session_can_handoff_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "codex", "修复会话接力")
            )

            self.assertEqual(plan.argv[0], "codex")
            self.assertNotIn("resume", plan.argv)
            self.assertIn("--add-dir", plan.argv)
            self.assertIn(str(history), plan.argv[-1])
            self.assertIn("修复会话接力", plan.argv[-1])
            self.assertIn("Claude Code JSONL", plan.argv[-1])
            self.assertEqual(plan.cwd, td)

    def test_codex_session_can_handoff_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "codex.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("codex", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "继续重构工具")
            )

            self.assertEqual(plan.argv[0], "claude")
            self.assertNotIn("--resume", plan.argv)
            self.assertIn("--add-dir", plan.argv)
            self.assertIn("Codex rollout JSONL", plan.argv[-1])

    def test_cross_runtime_requires_history_file(self) -> None:
        session = self._session("claude", "/tmp/missing-session-history.jsonl", os.getcwd())

        with self.assertRaisesRegex(LaunchError, "历史文件不存在"):
            default_registry().build_launch_plan(
                LaunchRequest(session, "codex", "修复会话接力")
            )

    def test_opencode_resume_plan(self) -> None:
        registry = default_registry()
        session = self._session("opencode", "/tmp/not-needed.db", "/tmp/not-exists")

        plan = registry.build_launch_plan(LaunchRequest(session, "opencode", "修复会话接力"))

        self.assertEqual(plan.argv, ("opencode", "-s", "session-123"))
        self.assertIsNone(plan.cwd)

    def test_opencode_continue_plan(self) -> None:
        registry = default_registry()
        session = self._session("opencode", "/tmp/not-needed.db", "/tmp/not-exists")

        plan = registry.get("opencode").build_continue_plan(session, "继续处理未完成的任务")

        self.assertEqual(
            plan.argv,
            ("opencode", "run", "--dangerously-skip-permissions", "-s", "session-123", "继续处理未完成的任务"),
        )

    def test_opencode_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("opencode", td))

        self.assertEqual(plan.argv, ("opencode",))
        self.assertEqual(plan.cwd, td)

    def test_claude_session_can_handoff_to_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "opencode", "修复会话接力")
            )

            self.assertEqual(plan.argv[0], "opencode")
            self.assertIn("--prompt", plan.argv)
            self.assertNotIn("-s", plan.argv)
            # OpenCode 主命令不接受 --dangerously-skip-permissions（实测非 run 子命令下会报错退出）
            self.assertNotIn("--dangerously-skip-permissions", plan.argv)
            self.assertIn("修复会话接力", plan.argv[-1])
            self.assertIn("Claude Code JSONL", plan.argv[-1])
            self.assertEqual(plan.cwd, td)

    def test_opencode_session_can_handoff_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_minimal_opencode_db(db_path, "ses_abc123", "修复登录")
            session = self._session("opencode", str(db_path), td)
            session["id"] = "ses_abc123"

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "继续接力")
            )

            self.assertEqual(plan.argv[0], "claude")
            self.assertIn("opencode export", plan.argv[-1])
            self.assertIn("ses_abc123", plan.argv[-1])

    def test_opencode_handoff_requires_db_file(self) -> None:
        session = self._session("opencode", "/tmp/missing-opencode.db", os.getcwd())

        with self.assertRaisesRegex(LaunchError, "历史文件不存在"):
            default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "修复会话接力")
            )

    def test_registry_accepts_new_runtime_without_pairwise_logic(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = registry.build_launch_plan(
                LaunchRequest(session, "gemini", "验证扩展能力")
            )

        self.assertEqual(registry.ids, ("claude", "codex", "opencode", "gemini"))
        self.assertEqual(plan.argv[0], "gemini")
        self.assertIn("验证扩展能力", plan.argv[-1])

    def test_claude_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("claude", td))

        self.assertEqual(plan.argv, ("claude", "--dangerously-skip-permissions"))
        self.assertEqual(plan.cwd, td)

    def test_codex_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("codex", td))

        self.assertEqual(
            plan.argv,
            ("codex", "-c", 'model_reasoning_effort="high"', "--dangerously-bypass-approvals-and-sandbox"),
        )
        self.assertEqual(plan.cwd, td)

    def test_new_session_plan_drops_nonexistent_cwd(self) -> None:
        plan = default_registry().build_new_session_plan(
            NewSessionRequest("claude", "/tmp/does-not-exist-sc-test")
        )

        self.assertIsNone(plan.cwd)

    def test_new_session_plan_dispatches_to_registered_runtime(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))
        with tempfile.TemporaryDirectory() as td:
            plan = registry.build_new_session_plan(NewSessionRequest("gemini", td))

        self.assertEqual(plan.argv, ("gemini",))
        self.assertEqual(plan.cwd, td)

    def test_passthrough_plan_prepends_auto_approve_args(self) -> None:
        plan = default_registry().build_passthrough_plan("claude", ["把测试修到全绿"])

        self.assertEqual(plan.argv, ("claude", "--dangerously-skip-permissions", "把测试修到全绿"))
        self.assertIsNone(plan.cwd)

    def test_passthrough_plan_does_not_duplicate_user_supplied_auto_approve_arg(self) -> None:
        plan = default_registry().build_passthrough_plan(
            "codex", ["--dangerously-bypass-approvals-and-sandbox", "resume"]
        )

        self.assertEqual(
            plan.argv,
            ("codex", "--dangerously-bypass-approvals-and-sandbox", "resume"),
        )

    def test_passthrough_plan_dispatches_to_registered_runtime(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))

        plan = registry.build_passthrough_plan("gemini", ["--foo"])

        self.assertEqual(plan.argv, ("gemini", "--foo"))
        self.assertIsNone(plan.cwd)

    def test_session_key_is_runtime_scoped(self) -> None:
        claude = {"source": "claude", "id": "same"}
        codex = {"source": "codex", "id": "same"}

        self.assertNotEqual(session_key(claude), session_key(codex))

    def test_generated_title_cache_is_runtime_scoped(self) -> None:
        sessions = [
            {
                "source": "claude",
                "id": "same",
                "size_bytes": 10,
                "size_kb": 0.1,
                "fallback_title": "Claude 任务",
            },
            {
                "source": "codex",
                "id": "same",
                "size_bytes": 20,
                "size_kb": 0.2,
                "fallback_title": "Codex 任务",
            },
        ]
        cache = {}
        generated = {"claude:same": "Claude 标题", "codex:same": "Codex 标题"}

        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=generated),
            mock.patch.object(titles, "save_cache", return_value=None),
        ):
            # 显式注入一个真值 generator：CI 环境没有安装 claude/codex，若依赖
            # refresh_titles 内部的 titlegen.resolve_generator() 自动探测，会在
            # 探测不到任何 CLI 时提前返回空字典，导致下面 mock 的
            # generate_titles_batch 根本不会被调用（本机因为装了 claude/codex，
            # 探测能成功，掩盖了这个问题，只有干净的 CI 环境才会暴露）。
            result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, generated)
        self.assertEqual(cache["claude:same"]["title"], "Claude 标题")
        self.assertEqual(cache["codex:same"]["title"], "Codex 标题")


if __name__ == "__main__":
    unittest.main()
