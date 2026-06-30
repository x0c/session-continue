from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import scan_claude
import scan_codex
import sc
import titles


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class TimezoneMixin:
    @classmethod
    def setUpClass(cls) -> None:
        cls._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        if hasattr(time, "tzset"):
            time.tzset()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._old_tz
        if hasattr(time, "tzset"):
            time.tzset()


class ClaudeScanTests(TimezoneMixin, unittest.TestCase):
    def test_extract_text_keeps_command_args_as_user_intent(self) -> None:
        content = (
            "<command-name>/plan</command-name>\n"
            "<command-message>plan</command-message>\n"
            "<command-args>@openconductor 页面输入框要支持文件上传和选择图片</command-args>"
        )

        self.assertEqual(
            scan_claude._extract_text(content),
            "@openconductor 页面输入框要支持文件上传和选择图片",
        )

    def test_title_uses_last_prompt_and_time_uses_file_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            rows = [
                {"type": "mode", "mode": "normal", "sessionId": "abc"},
                {
                    "type": "user",
                    "message": {"content": "第一条问题"},
                    "timestamp": "2026-06-22T08:11:26.000Z",
                    "cwd": "/tmp/demo",
                    "sessionId": "abc",
                },
            ]
            rows.extend({"type": "attachment", "sessionId": "abc"} for _ in range(8))
            rows.extend(
                [
                    {"type": "ai-title", "aiTitle": "分析代码索引更新机制", "sessionId": "abc"},
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "完成"}]},
                        "timestamp": "2026-06-22T08:41:55.000Z",
                        "sessionId": "abc",
                    },
                    {"type": "last-prompt", "lastPrompt": "最后一次问题", "sessionId": "abc"},
                ]
            )
            _write_jsonl(path, rows)
            os.utime(path, (1893456000, 1893456000))

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["native_title"], "分析代码索引更新机制")
            self.assertEqual(info["fallback_title"], "最后一次问题")
            self.assertEqual(info["display_time"], "01-01 08:00")
            self.assertEqual(info["mtime"], info["file_mtime"])
            self.assertEqual(scan_claude._format_display_time(info["event_time"]), "06-22 16:41")

    def test_scan_sessions_sorts_by_file_mtime_not_event_time(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                old_path = project / "old.jsonl"
                new_path = project / "new.jsonl"
                _write_jsonl(
                    old_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "旧会话"},
                            "timestamp": "2026-06-01T00:00:00.000Z",
                            "cwd": str(cwd),
                        }
                    ],
                )
                _write_jsonl(
                    new_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "新会话"},
                            "timestamp": "2026-06-25T00:00:00.000Z",
                            "cwd": str(cwd),
                        }
                    ],
                )
                os.utime(old_path, (1893456000, 1893456000))
                os.utime(new_path, (946684800, 946684800))

                sessions = scan_claude.scan_sessions(limit=2)

                self.assertEqual([s["fallback_title"] for s in sessions], ["旧会话", "新会话"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_ignores_bulk_touched_file_mtime(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                bulk_mtime = int(time.time() // 60) * 60
                for i in range(6):
                    path = project / f"bulk-{i}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "type": "user",
                                "message": {"content": f"批量旧会话 {i}"},
                                "timestamp": "2026-06-20T00:00:00.000Z",
                                "cwd": str(cwd),
                            }
                        ],
                    )
                    os.utime(path, (bulk_mtime, bulk_mtime))
                real_path = project / "real.jsonl"
                _write_jsonl(
                    real_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "真实新会话"},
                            "timestamp": "2026-06-25T00:00:00.000Z",
                            "cwd": str(cwd),
                        }
                    ],
                )
                os.utime(real_path, (bulk_mtime - 300, bulk_mtime - 300))

                sessions = scan_claude.scan_sessions(limit=7)

                self.assertEqual(sessions[0]["fallback_title"], "真实新会话")
                self.assertEqual(sessions[0]["time_source"], "file_mtime")
                self.assertTrue(all(s["time_source"] == "event_time_bulk_mtime" for s in sessions[1:]))
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_filters_and_sorts_before_applying_limit(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                old_path = Path(td) / "old_proj" / "old.jsonl"
                new_path = Path(td) / "new_proj" / "new.jsonl"
                _write_jsonl(
                    old_path,
                    [{"type": "user", "message": {"content": "旧会话"}, "timestamp": "2026-06-01T00:00:00.000Z"}],
                )
                _write_jsonl(
                    new_path,
                    [{"type": "user", "message": {"content": "新会话"}, "timestamp": "2026-06-01T00:00:00.000Z"}],
                )
                os.utime(old_path, (946684800, 946684800))
                os.utime(new_path, (1893456000, 1893456000))

                real_listdir = os.listdir

                def ordered_listdir(path: str) -> list[str]:
                    if path == td:
                        return ["old_proj", "new_proj"]
                    return real_listdir(path)

                with mock.patch.object(scan_claude.os, "listdir", side_effect=ordered_listdir):
                    sessions = scan_claude.scan_sessions(limit=1)

                self.assertEqual([s["fallback_title"] for s in sessions], ["新会话"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_claude_without_cached_title_triggers_background_generation(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "兜底标题",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "兜底标题")
        self.assertTrue(needs_gen)

    def test_claude_fallback_rejects_continue_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "帮我分析会话标题选择策略"},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {"type": "last-prompt", "lastPrompt": "继续", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "帮我分析会话标题选择策略")

    def test_claude_fallback_rejects_test_challenge_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                "<command-name>/plan</command-name>\n"
                                "<command-args>@openconductor 页面输入框要支持文件上传和选择图片</command-args>"
                            )
                        },
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {"type": "last-prompt", "lastPrompt": "你测试了吗 完整端到端测试", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "页面输入框要支持文件上传和选择图片")

    def test_claude_fallback_uses_agent_summary_when_user_prompts_are_low_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "[Request interrupted by user]"},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "端到端三步全通：上传、发送、回显均通过。"}]},
                        "timestamp": "2026-06-22T08:12:26.000Z",
                    },
                    {"type": "last-prompt", "lastPrompt": "快点啊", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "端到端三步全通：上传、发送、回显均通过。")

    def test_claude_native_slug_is_only_temporary_fallback(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "account-model-runtime-decoupling",
            "fallback_title": "继续",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "(待生成标题)")
        self.assertTrue(needs_gen)

    def test_claude_native_slug_does_not_override_meaningful_fallback(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "fix-doc-init-product-first",
            "fallback_title": "@doc-init/ @doc-update/ @doc-audit/ 关于现在的文档体系从产品视角优先",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "关于现在的文档体系从产品视角优先")
        self.assertTrue(needs_gen)

    def test_doc_command_fallback_is_human_readable_while_generation_is_pending(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "/doc-init @openconductor",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "openconductor 文档初始化")
        self.assertTrue(needs_gen)

    def test_generated_title_cache_wins_for_claude(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "fix-doc-init-product-first",
            "fallback_title": "/doc-init @openconductor",
        }

        title, needs_gen = titles.resolve_initial_title(
            session,
            {"abc": {"fp": "v3:1", "title": "文档体系产品化重构"}},
        )

        self.assertEqual(title, "文档体系产品化重构")
        self.assertFalse(needs_gen)

    def test_low_value_claude_session_is_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "..."},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": '{"019efe42-6d51-7fb3-ad48-112a8eefa02b": "修复会话时间显示与标题提取"}',
                                }
                            ]
                        },
                        "timestamp": "2026-06-22T08:12:26.000Z",
                    },
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "(仅本地命令)")

    def test_low_value_cached_title_is_ignored(self) -> None:
        session = {
            "source": "codex",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "真实问题标题",
        }

        title, needs_gen = titles.resolve_initial_title(session, {"abc": {"fp": "v3:1", "title": "继续"}})

        self.assertEqual(title, "真实问题标题")
        self.assertTrue(needs_gen)

    def test_cached_title_survives_file_mtime_change(self) -> None:
        old_session = {
            "source": "claude",
            "id": "abc",
            "mtime": 100,
            "size_bytes": 1234,
            "size_kb": 1.2,
            "native_title": "unstable-title-slug",
            "fallback_title": "真实会话意图",
        }
        new_session = dict(old_session, mtime=200)
        cache = {"abc": {"fp": titles._fingerprint(old_session), "title": "稳定生成标题"}}

        title, needs_gen = titles.resolve_initial_title(new_session, cache)

        self.assertEqual(title, "稳定生成标题")
        self.assertFalse(needs_gen)

    def test_stale_cached_title_is_displayed_while_refreshing(self) -> None:
        session = {
            "source": "codex",
            "id": "abc",
            "mtime": 200,
            "size_bytes": 2345,
            "size_kb": 2.3,
            "native_title": None,
            "fallback_title": "这是一条很长很长的用户原文，不应该因为会话文件继续增长就重新显示在列表里",
        }
        cache = {"abc": {"fp": "v3:1234", "title": "稳定生成标题"}}

        title, needs_gen = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "稳定生成标题")
        self.assertTrue(needs_gen)

    def test_refresh_titles_does_not_retry_each_session_when_batch_fails(self) -> None:
        sessions = [
            {"id": "a", "mtime": 1, "size_kb": 1, "fallback_title": "标题A"},
            {"id": "b", "mtime": 1, "size_kb": 1, "fallback_title": "标题B"},
        ]

        with mock.patch.object(titles, "generate_titles_batch", return_value={}) as mocked:
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, {})

        self.assertEqual(result, {})
        mocked.assert_called_once()
        save_mock.assert_not_called()

    def test_refresh_titles_saves_cache_per_batch(self) -> None:
        # 三批（_BATCH_SIZE 条/批），每批都有成功标题：应逐批落盘而非最后一次性写。
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1, "fallback_title": f"标题{i}"}
            for i in range(titles._BATCH_SIZE * 3)
        ]

        def fake_batch(chunk, model="haiku"):
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        with mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch):
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, {})

        self.assertEqual(len(result), titles._BATCH_SIZE * 3)
        self.assertEqual(save_mock.call_count, 3)


class CodexScanTests(TimezoneMixin, unittest.TestCase):
    def test_file_mtime_overrides_event_time_and_tail_keeps_first_line_for_small_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa02b"
            path = Path(td) / f"rollout-2026-06-25T18-10-26-{uuid}.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-06-25T10:10:40.522Z",
                        "type": "session_meta",
                        "payload": {
                            "id": uuid,
                            "timestamp": "2026-06-25T10:10:26.837Z",
                            "cwd": "/tmp/demo",
                        },
                    },
                    {
                        "timestamp": "2026-06-25T10:10:50.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "修复会话展示"},
                    },
                    {
                        "timestamp": "2026-06-25T10:12:15.876Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "last_agent_message": "已完成"},
                    },
                ],
            )
            os.utime(path, (946684800, 946684800))

            info = scan_codex._build_session_info(str(path), {})

            self.assertIsNotNone(info)
            self.assertEqual(info["display_time"], "01-01 08:00")
            self.assertEqual(scan_codex._format_display_time(info["event_time"]), "06-25 18:12")
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["first_user_msg"], "修复会话展示")


class ConversationPreviewTests(unittest.TestCase):
    def test_claude_conversation_keeps_users_and_end_turn_answers_only(self) -> None:
        entries = [
            {"type": "user", "message": {"content": "第一个问题"}},
            {
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "处理中间状态"}]},
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "工具结果"}]}},
            {
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第一个最终答复"}]},
            },
            {"type": "user", "isMeta": True, "message": {"content": "内部提醒"}},
            {"type": "user", "message": {"content": "第二个问题"}},
            {
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第二个最终答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [
                ("user", "第一个问题"),
                ("assistant", "第一个最终答复"),
                ("user", "第二个问题"),
                ("assistant", "第二个最终答复"),
            ],
        )

    def test_codex_conversation_uses_final_answer_and_removes_task_complete_duplicate(self) -> None:
        entries = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "用户问题"}},
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "commentary", "message": "处理中间状态"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": "最终答复"},
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "最终答复"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [("user", "用户问题"), ("assistant", "最终答复")],
        )


class TuiLayoutTests(unittest.TestCase):
    def test_session_store_uses_compact_title_before_background_generation(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "这是一条很长的兜底标题，启动后不应该直接显示",
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_sessions.return_value = [session]
        registry = sc.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(sc.titles, "load_cache", return_value={}):
            store = sc.SessionStore(limit=20, registry=registry)
            store.load()

        # 启动时无缓存：展示临时兜底标题，并标记为等待后台进程生成（转圈圈）。
        self.assertEqual(store.get_title(session), "这是一条很长的兜底标题")
        self.assertIn(sc.session_key(session), store.generating)

    def test_poll_cache_updates_clears_spinner_when_title_arrives(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "这是一条很长的兜底标题，启动后不应该直接显示",
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_sessions.return_value = [session]
        registry = sc.RuntimeRegistry((claude_runtime,))
        key = sc.session_key(session)

        with mock.patch.object(sc.titles, "load_cache", return_value={}):
            store = sc.SessionStore(limit=20, registry=registry)
            store.load()
        self.assertIn(key, store.generating)

        # 模拟后台进程把标题写进缓存：轮询应拾取它、刷新展示标题并停掉转圈圈。
        fresh_cache = {key: {"fp": titles._fingerprint(session), "title": "后台生成的标题"}}
        with (
            mock.patch.object(sc.SessionStore, "_cache_file_mtime", return_value=999.0),
            mock.patch.object(sc.titles, "load_cache", return_value=fresh_cache),
        ):
            store.poll_cache_updates()

        self.assertEqual(store.get_title(session), "后台生成的标题")
        self.assertNotIn(key, store.generating)
        self.assertTrue(store.dirty.is_set())

    def test_conversation_is_loaded_lazily_and_cached(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "测试会话",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        runtime.load_conversation.return_value = [sc.ConversationMessage("user", "问题")]
        registry = sc.RuntimeRegistry((runtime,))

        with mock.patch.object(sc.titles, "load_cache", return_value={}):
            store = sc.SessionStore(limit=20, registry=registry)
            store.load()

        runtime.load_conversation.assert_not_called()
        self.assertEqual(store.get_conversation(session), [sc.ConversationMessage("user", "问题")])
        self.assertEqual(store.get_conversation(session), [sc.ConversationMessage("user", "问题")])
        runtime.load_conversation.assert_called_once_with(session)

    def test_format_relative_time_thresholds(self) -> None:
        now = 1_000_000.0
        self.assertEqual(sc._format_relative_time(now - 5, now), "刚刚")
        self.assertEqual(sc._format_relative_time(now + 100, now), "刚刚")  # 时钟漂移/未来
        self.assertEqual(sc._format_relative_time(now - 120, now), "2分钟前")
        self.assertEqual(sc._format_relative_time(now - 3 * 3600, now), "3小时前")
        # 超过一天退回绝对日期时间（沿用 MM-DD HH:MM）
        old = now - 3 * 86400
        self.assertEqual(
            sc._format_relative_time(old, now),
            sc.datetime.fromtimestamp(old).strftime("%m-%d %H:%M"),
        )

    def test_fit_cell_uses_terminal_display_width(self) -> None:
        self.assertEqual(sc._text_width("标题"), 4)
        self.assertEqual(sc._fit_cell("标题", 6), "标题  ")
        self.assertEqual(sc._fit_cell("标题很长", 5), "标题 ")
        self.assertEqual(sc._text_width(sc._fit_cell("✅完成", 8)), 8)

    def test_preview_renders_messages_as_chronological_chat(self) -> None:
        messages = [
            sc.ConversationMessage("user", "请分析启动速度"),
            sc.ConversationMessage("assistant", "主要耗时来自历史扫描"),
            sc.ConversationMessage("user", "再增加聊天记录预览"),
            sc.ConversationMessage("assistant", "已经完成实现和验证"),
        ]

        lines = sc._preview_lines(messages, "Codex", 16)
        text_lines = [line for _, line in lines]

        self.assertEqual(text_lines[0], "● 你")
        self.assertIn("◆ Codex", text_lines)
        self.assertEqual(text_lines.count("● 你"), 2)
        self.assertEqual(text_lines.count("◆ Codex"), 2)
        self.assertTrue(all(sc._text_width(line) <= 16 for line in text_lines))

    def test_preview_is_a_centered_popup(self) -> None:
        top, left, height, width = sc._preview_geometry(40, 140)

        self.assertEqual((height, width), (28, 100))
        self.assertEqual((top, left), (6, 20))

    def test_directory_column_gets_more_space_on_normal_terminals(self) -> None:
        col_num, col_title, col_dir, col_time, col_size, col_status = sc._column_widths(120)

        self.assertEqual((col_num, col_time, col_size, col_status), (4, 17, 11, 10))
        self.assertGreaterEqual(col_title, 10)
        self.assertGreaterEqual(col_dir, 30)
        self.assertEqual(
            sum((col_num, col_title, col_dir, col_time, col_size, col_status)) + len(sc.COL_GAP) * 5,
            119,
        )


if __name__ == "__main__":
    unittest.main()
