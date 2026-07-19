from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

import scan_claude
import scan_codex
import scan_kimi
import scan_opencode
import pickup
import agent_api
import titles
from models import ConversationMessage, Handoff, LaunchPlan
from runtime.base import BaseRuntime
from runtime.claude import ClaudeRuntime
from runtime.codex import CodexRuntime


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _make_opencode_db(path: Path, sessions: list[dict], messages: list[dict] = (), parts: list[dict] = ()) -> None:
    """按 opencode.db 真实 schema（session/message/part 三表）建最小 SQLite fixture。

    sessions: 每项含 id/directory/title/time_created/time_updated，可选 parent_id/time_archived。
    messages: 每项含 id/session_id/time_created/data（dict，会被 json.dumps）。
    parts: 每项含 id/message_id/session_id/time_created/data（dict）。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE session (id text PRIMARY KEY, project_id text, parent_id text, "
            "slug text, directory text, title text, version text, "
            "time_created integer, time_updated integer, time_archived integer)"
        )
        conn.execute(
            "CREATE TABLE message (id text PRIMARY KEY, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        conn.execute(
            "CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        for s in sessions:
            conn.execute(
                "INSERT INTO session (id, project_id, parent_id, slug, directory, title, "
                "version, time_created, time_updated, time_archived) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    s["id"], s.get("project_id", "global"), s.get("parent_id"), s.get("slug", "x"),
                    s.get("directory", ""), s.get("title"), s.get("version", "1.0.0"),
                    s.get("time_created", 0), s.get("time_updated", 0), s.get("time_archived"),
                ),
            )
        for m in messages:
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                (m["id"], m["session_id"], m["time_created"], m.get("time_updated", m["time_created"]),
                 json.dumps(m["data"], ensure_ascii=False)),
            )
        for p in parts:
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?,?)",
                (p["id"], p["message_id"], p["session_id"], p["time_created"],
                 p.get("time_updated", p["time_created"]), json.dumps(p["data"], ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


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

    def test_extract_text_does_not_crash_when_part_text_is_json_null(self) -> None:
        # part.get("text", "") 的默认值只在 key 缺失时生效；key 存在但值为
        # JSON null 时曾直接 AttributeError（.strip() on None）。
        content = [{"type": "text", "text": None}, {"type": "text", "text": "真实文本"}]
        self.assertEqual(scan_claude._extract_text(content), "真实文本")

    def test_entry_time_does_not_crash_when_snapshot_is_json_null(self) -> None:
        self.assertIsNone(scan_claude._entry_time({"snapshot": None}))

    def test_build_session_info_does_not_crash_when_tail_assistant_text_is_json_null(self) -> None:
        # _build_session_info 的尾部循环里同样按 part.get("text", "").strip() 过滤
        # assistant 文本 part，text 为 JSON null 时曾直接 AttributeError。
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {"type": "user", "message": {"content": "问题"}, "cwd": td,
                     "timestamp": "2026-07-01T10:00:00Z"},
                    {"type": "assistant",
                     "message": {"content": [{"type": "text", "text": None},
                                              {"type": "text", "text": "真实答复"}]},
                     "timestamp": "2026-07-01T10:00:05Z"},
                ],
            )
            info = scan_claude._build_session_info(str(path), "proj")

        self.assertEqual(info["last_agent_msg"], "真实答复")

    def test_title_uses_last_prompt_and_time_falls_back_to_event_time_when_mtime_stale(self) -> None:
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
            # 文件 mtime 远远新于会话内最后一条真实事件（如驻留会话被重新打开后
            # 追加了 last-prompt/ai-title 等无时间戳元数据），应判定 mtime 不可信。
            os.utime(path, (1893456000, 1893456000))

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["native_title"], "分析代码索引更新机制")
            self.assertEqual(info["fallback_title"], "最后一次问题")
            self.assertEqual(info["display_time"], "06-22 16:41")
            self.assertEqual(info["mtime"], info["event_time"])
            self.assertEqual(info["time_source"], "event_time_stale_mtime")
            self.assertEqual(scan_claude._format_display_time(info["file_mtime"]), "01-01 08:00")

    def test_scan_sessions_sorts_by_effective_time(self) -> None:
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
                # old.jsonl 的 mtime 被顶到远未来（模拟被重新打开导致的假新），
                # 应回退到其真实事件时间 2026-06-01；new.jsonl 的 mtime 落在
                # 2000 年、比自己的事件时间 2026-06-25 还旧，不满足"mtime 比
                # 事件时间新"的回退条件，保持 file_mtime 不变。修正后 old.jsonl
                # 的有效时间（2026-06-01）仍晚于 new.jsonl 的有效时间（2000），
                # 排序结果与改动前巧合一致，但含义已从"纯按文件 mtime 排序"
                # 变为"按修正后的有效时间排序"。
                os.utime(old_path, (1893456000, 1893456000))
                os.utime(new_path, (946684800, 946684800))

                sessions = scan_claude.scan_sessions(limit=2)

                self.assertEqual([s["fallback_title"] for s in sessions], ["旧会话", "新会话"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_falls_back_to_event_time_for_bulk_touched_sessions(self) -> None:
        """Syncthing/复制等批量元数据刷新会让一批历史会话同一时刻被 touch；

        每个会话各自的 mtime 都远新于自己内部最后一条真实事件，单会话 gap
        规则应逐个回退到 event_time，不需要额外的"同分钟桶聚簇"判污染。
        """

        def _iso(ts: float) -> str:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

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
                                "timestamp": _iso(bulk_mtime - 10 * 86400),
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
                            "timestamp": _iso(bulk_mtime - 60),
                            "cwd": str(cwd),
                        }
                    ],
                )
                os.utime(real_path, (bulk_mtime - 60, bulk_mtime - 60))

                sessions = scan_claude.scan_sessions(limit=7)

                self.assertEqual(sessions[0]["fallback_title"], "真实新会话")
                self.assertEqual(sessions[0]["time_source"], "file_mtime")
                self.assertTrue(all(s["time_source"] == "event_time_stale_mtime" for s in sessions[1:]))
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_recovers_true_time_for_stale_resumed_session(self) -> None:
        """复现真实故障：会话内容 9 天前就结束，但驻留进程重新打开它时追加了

        没有时间戳的 last-prompt/ai-title/mode/permission-mode 元数据，把
        文件 mtime 顶到"现在"。列表时间列必须显示真实的 9 天前，而不是"刚刚"。
        """
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                now = time.time()
                stale_event = now - 9 * 86400
                path = project / "resumed.jsonl"
                _write_jsonl(
                    path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "9天前的真实问题"},
                            "timestamp": datetime.fromtimestamp(stale_event, tz=timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            ),
                            "cwd": str(cwd),
                        },
                        {"type": "last-prompt", "lastPrompt": "9天前的真实问题"},
                        {"type": "ai-title", "aiTitle": "旧任务"},
                        {"type": "mode", "mode": "normal"},
                        {"type": "permission-mode", "mode": "default"},
                    ],
                )
                os.utime(path, (now, now))  # 驻留会话被重新打开，文件 mtime 顶到现在

                sessions = scan_claude.scan_sessions(limit=1)

                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["time_source"], "event_time_stale_mtime")
                self.assertAlmostEqual(sessions[0]["mtime"], stale_event, delta=2)
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

    def test_low_value_only_session_does_not_trigger_generation(self) -> None:
        session = {
            "source": "opencode",
            "id": "only-greeting",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "在吗",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "在吗")
        self.assertFalse(needs_gen)

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

    def test_cached_title_is_not_regenerated_when_session_grows(self) -> None:
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
        self.assertFalse(needs_gen)

    def test_refresh_titles_does_not_retry_each_session_when_batch_fails(self) -> None:
        sessions = [
            {"id": "a", "mtime": 1, "size_kb": 1, "fallback_title": "标题A"},
            {"id": "b", "mtime": 1, "size_kb": 1, "fallback_title": "标题B"},
        ]
        cache = {}

        with mock.patch.object(titles, "generate_titles_batch", return_value={}) as mocked:
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, {})
        mocked.assert_called_once()
        save_mock.assert_called_once()
        for session in sessions:
            entry = cache[titles.session_key(session)]
            self.assertEqual(entry["generation_state"], "failed")
            self.assertEqual(entry["generation_version"], titles.TITLE_CACHE_VERSION)
            self.assertFalse(titles.resolve_initial_title(session, cache)[1])

        expired_cache = {
            titles.session_key(sessions[0]): {
                **cache[titles.session_key(sessions[0])],
                "generation_version": titles.TITLE_CACHE_VERSION - 1,
            }
        }
        self.assertTrue(titles.resolve_initial_title(sessions[0], expired_cache)[1])

    def test_refresh_titles_without_available_generator_writes_finished_state(self) -> None:
        sessions = [
            {"id": "offline", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "整理离线安装流程"},
        ]
        cache = {}

        with (
            mock.patch.object(titles.titlegen, "available_generators", return_value=()),
            mock.patch.object(titles, "save_cache") as save_mock,
        ):
            result = titles.refresh_titles(sessions, cache)

        self.assertEqual(result, {})
        save_mock.assert_called_once_with(cache)
        entry = cache["claude:offline"]
        self.assertEqual(entry["generation_state"], "failed")
        self.assertEqual(entry["generation_version"], titles.TITLE_CACHE_VERSION)
        self.assertEqual(titles.resolve_initial_title(sessions[0], cache), ("整理离线安装流程", False))

    def test_refresh_titles_partial_result_marks_missing_and_low_value_items_finished(self) -> None:
        sessions = [
            {"id": "ok", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "修复登录报错"},
            {"id": "missing", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "补充支付测试"},
            {"id": "low", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "整理部署流程"},
        ]
        cache = {}
        raw = {"claude:ok": "修复登录报错", "claude:low": "继续"}

        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, {"claude:ok": "修复登录报错"})
        self.assertNotIn("generation_state", cache["claude:ok"])
        for key in ("claude:missing", "claude:low"):
            self.assertEqual(cache[key]["generation_state"], "failed")
        self.assertFalse(titles.resolve_initial_title(sessions[1], cache)[1])
        self.assertFalse(titles.resolve_initial_title(sessions[2], cache)[1])

    def test_refresh_titles_probes_bad_preferred_generator_only_once(self) -> None:
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": f"处理标题任务{i}"}
            for i in range(titles._BATCH_SIZE * 3)
        ]
        failed = mock.Mock()
        failed.id = "claude"
        fallback = mock.Mock()
        fallback.id = "codex"
        calls = {"claude": 0, "codex": 0}

        def fake_batch(chunk, generator, timeout=90):
            calls[generator.id] += 1
            if generator is failed:
                return {}
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        with (
            mock.patch.object(titles.titlegen, "available_generators", return_value=(failed, fallback)),
            mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles(sessions, {})

        self.assertEqual(len(result), len(sessions))
        self.assertEqual(calls["claude"], 1)
        self.assertEqual(calls["codex"], 3)

    def test_refresh_titles_saves_cache_per_batch(self) -> None:
        # 三批（_BATCH_SIZE 条/批）并行完成后，仍应每批落盘而非最后一次性写。
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1, "fallback_title": f"标题{i}"}
            for i in range(titles._BATCH_SIZE * 3)
        ]

        def fake_batch(chunk, model="haiku"):
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        with mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch):
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, {}, generator=mock.Mock())

        self.assertEqual(len(result), titles._BATCH_SIZE * 3)
        self.assertEqual(save_mock.call_count, 3)

    def test_refresh_titles_runs_five_batches_in_parallel(self) -> None:
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1, "fallback_title": f"标题{i}"}
            for i in range(titles._BATCH_SIZE * (titles._MAX_PARALLEL_BATCHES + 1))
        ]
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        state = {"calls": 0, "active": 0, "max_active": 0}
        outcome = {}

        def fake_batch(chunk, generator, timeout=90):
            with lock:
                state["calls"] += 1
                # 第一批是串行健康探测，必须先正常完成；后面的五批才并发。
                if state["calls"] == 1:
                    return {titles.session_key(s): f"生成{s['id']}" for s in chunk}
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["active"] == titles._MAX_PARALLEL_BATCHES:
                    started.set()
            if not release.wait(timeout=2):
                raise AssertionError("并行批次未在预期时间内释放")
            with lock:
                state["active"] -= 1
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        def run_refresh():
            try:
                outcome["result"] = titles.refresh_titles(sessions, {}, generator=mock.Mock())
            except BaseException as exc:
                outcome["error"] = exc

        with (
            mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch),
            mock.patch.object(titles, "save_cache", return_value=None),
        ):
            runner = threading.Thread(target=run_refresh)
            runner.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                with lock:
                    self.assertEqual(state["active"], titles._MAX_PARALLEL_BATCHES)
                    self.assertEqual(state["max_active"], titles._MAX_PARALLEL_BATCHES)
            finally:
                release.set()
            runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(len(outcome["result"]), len(sessions))
        self.assertEqual(state["calls"], titles._MAX_PARALLEL_BATCHES + 1)

    def test_scan_sessions_memoizes_cwd_isdir_and_peek_skips_noise_and_dead_cwd(self) -> None:
        # 首屏 ≤1s 的回归防退化用例：不依赖真实数据。构造大量会话共享极少数
        # cwd，断言 (1) 内容只保留真实会话且排序正确；(2) os.path.isdir 按 cwd
        # 记忆化，不随会话条数线性增长；(3) 廉价预探提前跳过噪音/死 cwd 会话，
        # 不必等整文件解析完才丢弃。
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as workspace:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                # cwd 路径特意放在 PROJECTS_DIR 之外的独立目录树，避免被目录遍历
                # 循环自身的 os.path.isdir(proj_base) 检查提前访问，干扰调用计数。
                real_cwd = Path(workspace) / "real_cwd"
                real_cwd.mkdir()
                dead_cwd = str(Path(workspace) / "dead_cwd_does_not_exist")

                base_mtime = 1_800_000_000
                file_index = 0

                def write_session(content: str, cwd: str, minute_offset: int) -> Path:
                    nonlocal file_index
                    file_index += 1
                    path = project / f"s{file_index}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "type": "user",
                                "message": {"content": content},
                                "timestamp": "2026-06-22T08:11:26.000Z",
                                "cwd": cwd,
                            }
                        ],
                    )
                    mtime = base_mtime + minute_offset * 120  # 分钟桶两两不同，避免污染检测分支
                    os.utime(path, (mtime, mtime))
                    return path

                for i in range(5):
                    write_session(f"真实问题 {i}", str(real_cwd), minute_offset=i)
                noise_paths = [
                    write_session(f"{titles.PROMPT_MARKER} 摘录 {i}", str(real_cwd), minute_offset=10 + i)
                    for i in range(4)
                ]
                dead_cwd_paths = [
                    write_session(f"真实问题但目录已删 {i}", dead_cwd, minute_offset=20 + i)
                    for i in range(3)
                ]
                for i in range(2):
                    write_session_empty_path = project / f"empty{i}.jsonl"
                    _write_jsonl(
                        write_session_empty_path,
                        [{"type": "mode", "mode": "normal"}],
                    )
                    mtime = base_mtime + (30 + i) * 120
                    os.utime(write_session_empty_path, (mtime, mtime))

                real_isdir = scan_claude.os.path.isdir
                isdir_calls: list[str] = []

                def counting_isdir(path: str) -> bool:
                    isdir_calls.append(path)
                    return real_isdir(path)

                real_build = scan_claude._build_session_info
                build_calls: list[str] = []

                def counting_build(fpath: str, proj: str):
                    build_calls.append(fpath)
                    return real_build(fpath, proj)

                with (
                    mock.patch.object(scan_claude.os.path, "isdir", side_effect=counting_isdir),
                    mock.patch.object(scan_claude, "_build_session_info", side_effect=counting_build),
                ):
                    sessions = scan_claude.scan_sessions(limit=10)
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

        # 1) 内容正确：只剩 5 条真实会话，噪音/死 cwd/空会话全部被过滤，按 mtime 降序。
        self.assertEqual(len(sessions), 5)
        self.assertEqual(
            [s["fallback_title"] for s in sessions],
            [f"真实问题 {i}" for i in range(4, -1, -1)],
        )

        # 2) 判活去重生效：5 个真实会话 + 4 个噪音会话都引用同一个 real_cwd，
        #    但真正落到 os.path.isdir(real_cwd) 的调用只有 1 次（记忆化）；
        #    3 个死 cwd 会话同理只触发 1 次。
        self.assertEqual(isdir_calls.count(str(real_cwd)), 1)
        self.assertEqual(isdir_calls.count(dead_cwd), 1)

        # 3) 预探跳噪音生效：噪音（4）和死 cwd（3）会话的完整解析被提前拦截，
        #    整文件解析只发生在 5 个真实会话 + 2 个空会话（peek 探测不到首条
        #    用户消息，只能落回完整解析兜底）身上，一共 7 次，而不是 5+4+3+2=14。
        self.assertEqual(len(build_calls), 7)
        for path in noise_paths + dead_cwd_paths:
            self.assertNotIn(str(path), build_calls)

    def test_live_session_ids_matches_alive_pid_and_skips_dead_pid(self) -> None:
        # 状态列判活：只有 pid 文件里的进程真的还存活才算 live。
        with tempfile.TemporaryDirectory() as td:
            alive_pid = os.getpid()  # 当前测试进程本身，保证存活
            dead_pid = 99999999  # 大概率不存在的 pid
            (Path(td) / f"{alive_pid}.json").write_text(
                json.dumps({"sessionId": "alive-session"}), encoding="utf-8"
            )
            (Path(td) / f"{dead_pid}.json").write_text(
                json.dumps({"sessionId": "dead-session"}), encoding="utf-8"
            )

            old_sessions_dir = scan_claude.SESSIONS_DIR
            scan_claude.SESSIONS_DIR = td
            try:
                live_ids = scan_claude._live_session_ids()
            finally:
                scan_claude.SESSIONS_DIR = old_sessions_dir

        self.assertIn("alive-session", live_ids)
        self.assertNotIn("dead-session", live_ids)
        self.assertEqual(live_ids["alive-session"], alive_pid)  # 判活的同时要能精确回填 pid


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

    def test_build_session_info_does_not_crash_when_payload_is_json_null(self) -> None:
        # entry.get("payload", {}) 的默认值只在 key 缺失时生效；某些事件类型的
        # payload 字段可能是 JSON null（key 存在但值为 null），曾在后续
        # .get(...) 上直接 AttributeError 崩掉整个扫描。
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa03c"
            path = Path(td) / f"rollout-2026-06-25T18-10-26-{uuid}.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-06-25T10:10:40.522Z",
                        "type": "session_meta",
                        "payload": {"id": uuid, "cwd": "/tmp/demo"},
                    },
                    {"timestamp": "2026-06-25T10:10:45.000Z", "type": "noise_event", "payload": None},
                    {
                        "timestamp": "2026-06-25T10:10:50.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "真实用户消息"},
                    },
                ],
            )

            info = scan_codex._build_session_info(str(path), {})

        self.assertIsNotNone(info)
        self.assertEqual(info["first_user_msg"], "真实用户消息")

    def test_entry_time_does_not_crash_when_payload_is_json_null(self) -> None:
        self.assertIsNone(scan_codex._entry_time({"payload": None}))

    def test_recovers_true_time_when_stale_session_file_gets_touched(self) -> None:
        """复现真实故障的 Codex 版本：会话内容 9 天前结束，文件之后被 touch

        （如同步、复制）把 mtime 顶到"现在"；时间列必须回退到真实事件时间。
        """
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa02c"
            now = time.time()
            stale_event = now - 9 * 86400
            stale_dt = datetime.fromtimestamp(stale_event, tz=timezone.utc)
            path = Path(td) / f"rollout-{stale_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid}.jsonl"
            iso = stale_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": iso,
                        "type": "session_meta",
                        "payload": {"id": uuid, "timestamp": iso, "cwd": "/tmp/demo"},
                    },
                    {
                        "timestamp": iso,
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "9天前的真实问题"},
                    },
                ],
            )
            os.utime(path, (now, now))

            info = scan_codex._build_session_info(str(path), {})

            self.assertIsNotNone(info)
            self.assertEqual(info["time_source"], "event_time_stale_mtime")
            self.assertAlmostEqual(info["mtime"], stale_event, delta=2)

    def test_scan_sessions_memoizes_cwd_isdir_check(self) -> None:
        # 首屏 ≤1s 回归防退化：多个会话共享同一个 cwd 时，os.path.isdir 只应
        # 被真正调用一次（记忆化），不随会话条数线性增长。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")  # 不存在，_load_index 返回空
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()

                for i in range(5):
                    uuid = f"019efe42-6d51-7fb3-ad48-112a8eefa0{i:02d}"
                    path = Path(td) / f"rollout-2026-06-25T18-1{i}-26-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-06-25T10:10:26.837Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": str(real_cwd)},
                            },
                            {
                                "timestamp": "2026-06-25T10:10:50.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": f"真实问题 {i}"},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 120
                    os.utime(path, (mtime, mtime))

                real_isdir = scan_codex.os.path.isdir
                isdir_calls: list[str] = []

                def counting_isdir(path: str) -> bool:
                    isdir_calls.append(path)
                    return real_isdir(path)

                with mock.patch.object(scan_codex.os.path, "isdir", side_effect=counting_isdir):
                    sessions = scan_codex.scan_sessions(limit=10)
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual(len(sessions), 5)
        self.assertEqual(isdir_calls.count(str(real_cwd)), 1)

    def test_scan_sessions_filters_out_subagent_thread_spawns(self) -> None:
        # 复现真实故障：Codex 多智能体任务会把每个子代理线程写成独立的
        # rollout 文件，且 fork 时继承父会话开头的历史，导致同一段用户消息
        # 在列表里重复出现好几条。session_meta.payload.thread_source
        # == "subagent" 的文件不是用户发起的顶层会话，必须被过滤掉。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")

                root_uuid = "019f4b05-4930-7c31-9920-86c9f7a7570d"
                root_path = Path(td) / f"rollout-2026-07-10T15-54-25-{root_uuid}.jsonl"
                _write_jsonl(
                    root_path,
                    [
                        {
                            "timestamp": "2026-07-10T07:54:25.456Z",
                            "type": "session_meta",
                            "payload": {"id": root_uuid, "cwd": str(Path(td))},
                        },
                        {
                            "timestamp": "2026-07-10T07:54:26.000Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "原始需求"},
                        },
                    ],
                )
                os.utime(root_path, (1_800_000_000, 1_800_000_000))

                subagent_uuid = "019f4b30-fba4-75d1-abd1-d086ddd1699c"
                subagent_path = Path(td) / f"rollout-2026-07-10T16-42-09-{subagent_uuid}.jsonl"
                _write_jsonl(
                    subagent_path,
                    [
                        {
                            "timestamp": "2026-07-10T08:42:09.349Z",
                            "type": "session_meta",
                            "payload": {
                                "id": subagent_uuid,
                                "forked_from_id": root_uuid,
                                "cwd": str(Path(td)),
                                "thread_source": "subagent",
                                "agent_nickname": "Parfit",
                            },
                        },
                        {
                            "timestamp": "2026-07-10T08:42:09.400Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "原始需求"},
                        },
                    ],
                )
                os.utime(subagent_path, (1_800_000_120, 1_800_000_120))

                sessions = scan_codex.scan_sessions(limit=10)
            self.assertEqual([s["id"] for s in sessions], [root_uuid])
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

    def test_live_session_ids_parses_uuid_from_proc_fd_on_linux(self) -> None:
        # Linux 判活：codex 进程持有自己的 rollout jsonl 文件描述符，遍历
        # /proc/<pid>/fd 逐个 readlink 抽出会话 UUID 即视为存活。
        uuid = "019f2c27-c9b0-7dc3-a600-8678bf0e8dcc"
        rollout_path = (
            f"/home/user/.codex/sessions/2026/07/04/rollout-2026-07-04T16-03-52-{uuid}.jsonl"
        )

        def fake_check_output(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return b"47372\n"
            raise AssertionError(f"unexpected command: {cmd}")

        def fake_listdir(path):
            if path == "/proc/47372/fd":
                return ["0", "1", "2", "45"]
            raise AssertionError(f"unexpected listdir: {path}")

        def fake_readlink(path):
            if path == "/proc/47372/fd/45":
                return rollout_path
            raise OSError("not a symlink we care about")

        with mock.patch("scan_codex.subprocess.check_output", side_effect=fake_check_output), \
             mock.patch("scan_codex.sys.platform", "linux"), \
             mock.patch("scan_codex.os.listdir", side_effect=fake_listdir), \
             mock.patch("scan_codex.os.readlink", side_effect=fake_readlink):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {uuid: 47372})  # 判活的同时要能精确回填 pid

    def test_live_session_ids_parses_uuid_from_lsof_on_macos(self) -> None:
        # macOS（无 /proc）判活：一次合并 lsof -Fpn 调用覆盖全部候选 pid，
        # 按 p<pid>/n<name> 字段行重建 pid -> 文件名对应关系。
        uuid = "019f2c27-c9b0-7dc3-a600-8678bf0e8dcc"
        lsof_output = (
            "p47372\n"
            f"n/private/tmp/codex/sessions/2026/07/04/"
            f"rollout-2026-07-04T16-03-52-{uuid}.jsonl\n"
        )

        def fake_check_output(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return b"47372\n"
            if cmd[0] == "lsof":
                self.assertEqual(cmd, ["lsof", "-n", "-P", "-Fpn", "-p", "47372"])
                return lsof_output.encode()
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch("scan_codex.subprocess.check_output", side_effect=fake_check_output), \
             mock.patch("scan_codex.sys.platform", "darwin"):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {uuid: 47372})  # 判活的同时要能精确回填 pid

    def test_live_session_ids_returns_empty_when_pgrep_unavailable(self) -> None:
        # pgrep 缺失或调用失败时静默降级为空集，不抛异常。
        with mock.patch(
            "scan_codex.subprocess.check_output", side_effect=FileNotFoundError()
        ):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {})

    def test_scan_filters_self_generated_title_sessions(self) -> None:
        # 后台标题生成兜底路径若真在 ~/.codex/sessions/ 留下会话
        # (旧版 codex 无 --ephemeral,或用户手动跑过同款 prompt),
        # 必须像 Claude 侧一样按 PROMPT_MARKER 前缀过滤,不进列表。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()

                specs = [
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa01", "真实的用户问题"),
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa02", f"{titles.PROMPT_MARKER}(JSON 数组…)"),
                ]
                for i, (uuid, first_msg) in enumerate(specs):
                    path = Path(td) / f"rollout-2026-07-16T10-0{i}-00-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-07-16T02:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": str(real_cwd)},
                            },
                            {
                                "timestamp": "2026-07-16T02:00:10.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": first_msg},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 60
                    os.utime(path, (mtime, mtime))

                sessions = scan_codex.scan_sessions(limit=10)
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual([s["first_user_msg"] for s in sessions], ["真实的用户问题"])


class OpenCodeScanTests(TimezoneMixin, unittest.TestCase):
    """OpenCode 历史存 SQLite（session/message/part 三表），扫描用只读连接直接查询。"""

    def _text_part(self, part_id: str, message_id: str, session_id: str, text, t: int, synthetic: bool = False) -> dict:
        data = {"type": "text", "text": text}
        if synthetic:
            data["synthetic"] = True
        return {"id": part_id, "message_id": message_id, "session_id": session_id, "time_created": t, "data": data}

    def test_field_mapping_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_a", "directory": "/tmp/demo", "title": "修复登录",
                    "time_created": 1_700_000_000_000, "time_updated": 1_700_000_500_000,
                }],
                messages=[
                    {"id": "msg_u1", "session_id": "ses_a", "time_created": 1_700_000_000_000,
                     "data": {"role": "user", "time": {"created": 1_700_000_000_000}}},
                    {"id": "msg_a1", "session_id": "ses_a", "time_created": 1_700_000_500_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 1_700_000_500_000}}},
                ],
                parts=[
                    self._text_part("p1", "msg_u1", "ses_a", "帮我修复登录报错", 1_700_000_000_000),
                    self._text_part("p2", "msg_a1", "ses_a", "已定位并修复", 1_700_000_500_000),
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual(len(sessions), 1)
            info = sessions[0]
            self.assertEqual(info["source"], "opencode")
            self.assertEqual(info["id"], "ses_a")
            self.assertEqual(info["short_id"], "ses_a"[:12])
            self.assertEqual(info["cwd"], "/tmp/demo")
            self.assertEqual(info["native_title"], "修复登录")
            self.assertEqual(info["fallback_title"], "帮我修复登录报错")
            self.assertEqual(info["mtime"], 1_700_000_500_000 / 1000)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["size_bytes"], len(json.dumps({"type": "text", "text": "帮我修复登录报错"}, ensure_ascii=False))
                              + len(json.dumps({"type": "text", "text": "已定位并修复"}, ensure_ascii=False)))
            self.assertEqual(info["path"], str(db_path))

    def test_filters_out_subagent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_root", "directory": "/tmp/demo", "title": "根会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_sub", "directory": "/tmp/demo", "title": "子代理会话",
                     "parent_id": "ses_root", "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_root"])

    def test_filters_out_archived_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_live", "directory": "/tmp/demo", "title": "存活会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_arch", "directory": "/tmp/demo", "title": "已归档会话",
                     "time_archived": 999_999, "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_live"])

    def test_empty_session_without_title_is_dropped_but_native_title_alone_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_blank", "directory": "/tmp/demo", "title": None,
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_titled_only", "directory": "/tmp/demo", "title": "简单问候",
                     "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_titled_only"])
            self.assertEqual(sessions[0]["fallback_title"], "(无消息)")

    def test_status_tag_all_branches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_pending", "directory": "/tmp/a", "title": "待回复",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_done", "directory": "/tmp/b", "title": "已完成",
                     "time_created": 0, "time_updated": 200_000},
                    {"id": "ses_aborted", "directory": "/tmp/c", "title": "已中断",
                     "time_created": 0, "time_updated": 300_000},
                    {"id": "ses_none", "directory": "/tmp/d", "title": "无状态",
                     "time_created": 0, "time_updated": 400_000},
                ],
                messages=[
                    {"id": "m_pending", "session_id": "ses_pending", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_done", "session_id": "ses_done", "time_created": 200_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 200_000}}},
                    {"id": "m_aborted", "session_id": "ses_aborted", "time_created": 300_000,
                     "data": {"role": "assistant", "error": {"message": "连接失败"}, "time": {"created": 300_000}}},
                    {"id": "m_none", "session_id": "ses_none", "time_created": 400_000,
                     "data": {"role": "assistant", "finish": "tool-calls", "time": {"created": 400_000}}},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            by_id = {s["id"]: s["status_tag"] for s in sessions}
            self.assertEqual(by_id["ses_pending"], titles.STATUS_PENDING)
            self.assertEqual(by_id["ses_done"], titles.STATUS_DONE)
            self.assertEqual(by_id["ses_aborted"], titles.STATUS_ABORTED)
            self.assertEqual(by_id["ses_none"], titles.STATUS_NONE)

    def test_chinese_native_title_and_preview_survive_intact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_cn", "directory": "/tmp/demo", "title": "中文标题：修复终端乱码问题",
                    "time_created": 0, "time_updated": 100_000,
                }],
                messages=[
                    {"id": "m1", "session_id": "ses_cn", "time_created": 0,
                     "data": {"role": "user", "time": {"created": 0}}},
                ],
                parts=[self._text_part("p1", "m1", "ses_cn", "终端显示的中文全是乱码，帮我看看", 0)],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual(sessions[0]["native_title"], "中文标题：修复终端乱码问题")
            self.assertEqual(sessions[0]["fallback_title"], "终端显示的中文全是乱码，帮我看看")

    def test_scan_sessions_returns_empty_when_db_missing(self) -> None:
        with mock.patch.object(scan_opencode, "_db_paths", return_value=[]):
            self.assertEqual(scan_opencode.scan_sessions(limit=10), [])

    def test_scan_signature_sorts_live_snapshot_and_detects_process_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            db_path.touch()

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name",
                                   return_value={"/tmp/b": 22, "/tmp/a": 11}):
                first = scan_opencode.scan_signature()
            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name",
                                   return_value={"/tmp/a": 11, "/tmp/b": 22}):
                reordered = scan_opencode.scan_signature()
            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name",
                                   return_value={"/tmp/a": 11}):
                changed = scan_opencode.scan_signature()

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_scan_sessions_raises_when_all_existing_db_connections_fail(self) -> None:
        with mock.patch.object(scan_opencode, "_db_paths", return_value=["a.db", "b.db"]), \
             mock.patch.object(scan_opencode, "_connect_ro", return_value=None), \
             mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                scan_opencode.scan_sessions(limit=10)

    def test_scan_sessions_keeps_successful_result_when_another_db_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broken_path = Path(td) / "broken.db"
            broken_path.write_text("不是一个真正的 sqlite 文件", encoding="utf-8")
            healthy_path = Path(td) / "healthy.db"
            _make_opencode_db(
                healthy_path,
                sessions=[{
                    "id": "ses_healthy", "directory": "/tmp/demo", "title": "正常会话",
                    "time_created": 0, "time_updated": 100_000,
                }],
            )

            with mock.patch.object(
                scan_opencode, "_db_paths", return_value=[str(broken_path), str(healthy_path)]
            ), mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=10)

        self.assertEqual([session["id"] for session in sessions], ["ses_healthy"])

    def test_scan_sessions_raises_when_db_is_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            db_path.write_text("不是一个真正的 sqlite 文件", encoding="utf-8")

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                    scan_opencode.scan_sessions(limit=10)

    def test_scan_sessions_raises_when_tables_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            conn = sqlite3.connect(str(db_path))
            conn.close()  # 建一个空库，session/message/part 表都不存在

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                    scan_opencode.scan_sessions(limit=10)

    def test_limit_keeps_newest_sessions_in_descending_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": f"ses_{i}", "directory": "/tmp/demo", "title": f"会话{i}",
                     "time_created": 0, "time_updated": i * 100_000}
                    for i in range(5)
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_pids_by_process_name", return_value={}):
                sessions = scan_opencode.scan_sessions(limit=3)

            self.assertEqual([s["id"] for s in sessions], ["ses_4", "ses_3", "ses_2"])

    def test_live_backfill_only_marks_newest_session_in_same_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_old", "directory": str(cwd_dir), "title": "旧会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_new", "directory": str(cwd_dir), "title": "新会话",
                     "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(
                     scan_opencode, "live_pids_by_process_name",
                     return_value={os.path.realpath(str(cwd_dir)): 4242},
                 ):
                sessions = scan_opencode.scan_sessions(limit=10)

            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id["ses_new"]["live"])
            self.assertEqual(by_id["ses_new"]["pid"], 4242)
            self.assertFalse(by_id["ses_old"]["live"])
            self.assertIsNone(by_id["ses_old"]["pid"])

    def test_live_pids_by_cwd_degrades_when_pgrep_unavailable(self) -> None:
        with mock.patch("scan_common.subprocess.check_output", side_effect=FileNotFoundError()):
            self.assertEqual(scan_opencode.live_pids_by_process_name("opencode"), {})

    def test_load_conversation_merges_parts_filters_roles_and_avoids_none_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{"id": "ses_multi", "directory": "/tmp/demo", "title": "多段消息",
                           "time_created": 0, "time_updated": 300_000}],
                messages=[
                    {"id": "m_user", "session_id": "ses_multi", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_asst", "session_id": "ses_multi", "time_created": 200_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 200_000}}},
                    {"id": "m_null", "session_id": "ses_multi", "time_created": 300_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 300_000}}},
                ],
                parts=[
                    self._text_part("p_user", "m_user", "ses_multi", "先看看这个报错", 100_000),
                    # 同一条 assistant 消息的两段 text part（step 之间产生），应合并成一条
                    self._text_part("p_a1", "m_asst", "ses_multi", "第一段结论", 200_000),
                    self._text_part("p_a2", "m_asst", "ses_multi", "第二段结论", 200_001),
                    # 非 text 类型的 part 不应出现在对话里
                    {"id": "p_reason", "message_id": "m_asst", "session_id": "ses_multi",
                     "time_created": 200_002, "data": {"type": "reasoning", "text": "思考过程"}},
                    {"id": "p_tool", "message_id": "m_asst", "session_id": "ses_multi",
                     "time_created": 200_003, "data": {"type": "tool", "tool": "bash"}},
                    # synthetic 注入内容应被过滤
                    self._text_part("p_synthetic", "m_asst", "ses_multi", "系统注入内容", 200_004, synthetic=True),
                    # text 为 JSON null 时不应产出字面量 "None"
                    self._text_part("p_null", "m_null", "ses_multi", None, 300_000),
                ],
            )

            messages = scan_opencode.load_conversation(str(db_path), "ses_multi")

            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].text, "先看看这个报错")
            self.assertEqual(messages[1].text, "第一段结论\n\n第二段结论")
            self.assertNotIn("None", messages[1].text)
            self.assertEqual(messages[0].timestamp, 100_000 / 1000)
            self.assertEqual(messages[1].timestamp, 200_000 / 1000)
            ts = [m.timestamp for m in messages if m.timestamp is not None]
            self.assertEqual(ts, sorted(ts))


def _make_kimi_session(sessions_dir: Path, workspace: str, session_id: str,
                       state: dict, wire_rows: list[dict]) -> Path:
    """在临时 sessions 目录下落一份 Kimi 会话（state.json + agents/main/wire.jsonl）。"""
    session_dir = sessions_dir / workspace / session_id
    (session_dir / "agents" / "main").mkdir(parents=True, exist_ok=True)
    (session_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in wire_rows) + "\n", encoding="utf-8")
    return session_dir


def _kimi_user_event(text: str, t: int, origin_kind: str = "user") -> dict:
    return {"type": "context.append_message", "time": t,
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "origin": {"kind": origin_kind}}}


def _kimi_assistant_text(text: str, t: int) -> dict:
    return {"type": "context.append_loop_event", "time": t,
            "event": {"type": "content.part", "part": {"type": "text", "text": text}}}


def _kimi_assistant_think(text: str, t: int) -> dict:
    return {"type": "context.append_loop_event", "time": t,
            "event": {"type": "content.part", "part": {"type": "think", "think": text}}}


class KimiScanTests(TimezoneMixin, unittest.TestCase):
    """Kimi Code 历史按「工作区 / 会话」两级目录存放，元数据取 state.json，正文解析 wire.jsonl。"""

    def _scan(self, sessions_dir: Path, **kwargs) -> list[dict]:
        with mock.patch.object(scan_kimi, "SESSIONS_DIR", str(sessions_dir)), \
             mock.patch.object(scan_kimi, "live_pids_by_process_name", return_value={}):
            return scan_kimi.scan_sessions(**kwargs)

    def test_field_mapping_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_abc",
                state={"title": "修复登录", "workDir": td, "lastPrompt": "在吗",
                       "createdAt": "2026-07-17T08:00:00.000Z", "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[
                    {"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000},
                    {"type": "config.update", "systemPrompt": "You are Kimi " * 500, "time": 1_784_275_200_000},
                    _kimi_user_event("帮我修复登录报错", 1_784_275_205_000),
                    _kimi_assistant_think("先看一下报错栈", 1_784_275_206_000),
                    _kimi_assistant_text("已定位并修复", 1_784_275_207_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)

            self.assertEqual(len(sessions), 1)
            info = sessions[0]
            self.assertEqual(info["source"], "kimi")
            self.assertEqual(info["id"], "session_abc")
            self.assertEqual(info["short_id"], "abc")
            self.assertEqual(info["cwd"], td)
            self.assertEqual(info["native_title"], "修复登录")
            self.assertEqual(info["fallback_title"], "修复登录")
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["first_user_msg"], "帮我修复登录报错")
            self.assertEqual(info["last_agent_msg"], "已定位并修复")
            self.assertEqual(info["path"], str(sessions_dir / "wd_demo" / "session_abc" / "agents" / "main" / "wire.jsonl"))
            # updatedAt 权威时间优先于文件 mtime
            self.assertEqual(info["mtime"], scan_kimi._parse_iso("2026-07-17T08:05:00.000Z"))

    def test_pending_status_when_last_event_is_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_pending",
                state={"title": "待回复", "workDir": td, "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[
                    _kimi_user_event("第一个问题", 1_784_275_205_000),
                    _kimi_assistant_text("这是回答", 1_784_275_206_000),
                    _kimi_user_event("追问一下", 1_784_275_207_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual(sessions[0]["status_tag"], titles.STATUS_PENDING)
            self.assertEqual(sessions[0]["last_user_msg"], "追问一下")

    def test_empty_session_is_dropped_but_title_only_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_blank",
                state={"title": None, "workDir": td, "updatedAt": "2026-07-17T08:01:00.000Z"},
                wire_rows=[{"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000}],
            )
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_titled",
                state={"title": "简单问候", "workDir": td, "updatedAt": "2026-07-17T08:02:00.000Z"},
                wire_rows=[{"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000}],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual([s["id"] for s in sessions], ["session_titled"])
            self.assertEqual(sessions[0]["fallback_title"], "简单问候")

    def test_blank_title_and_last_prompt_do_not_crash_scan(self) -> None:
        # 真实故障复现：title/lastPrompt 是纯空白字符串（如 " "）而非缺失时，
        # strip() 后为空串，splitlines()[0] 曾抛 IndexError 崩掉整个扫描。
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_blank_title",
                state={"title": " ", "workDir": td, "lastPrompt": "\n",
                       "updatedAt": "2026-07-17T08:01:00.000Z"},
                wire_rows=[
                    _kimi_user_event("真实用户消息", 1_784_275_205_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual([s["id"] for s in sessions], ["session_blank_title"])
            self.assertEqual(sessions[0]["fallback_title"], "真实用户消息")

    def test_dead_cwd_session_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_dead",
                state={"title": "旧任务", "workDir": "/tmp/does-not-exist-kimi-test",
                       "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[_kimi_user_event("你好", 1_784_275_205_000)],
            )
            self.assertEqual(self._scan(sessions_dir, limit=10), [])

    def test_scan_returns_empty_when_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._scan(Path(td) / "nope", limit=10), [])

    def test_load_conversation_filters_think_and_merges_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wire = Path(td) / "wire.jsonl"
            _write_jsonl(wire, [
                {"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000},
                {"type": "config.update", "systemPrompt": "system noise", "time": 1_784_275_200_000},
                _kimi_user_event("先看看这个报错", 100_000),
                _kimi_assistant_think("我先想想", 150_000),
                _kimi_assistant_text("第一段结论", 200_000),
                _kimi_assistant_text("第二段结论", 201_000),
                _kimi_user_event("好的继续", 300_000),
                _kimi_assistant_text("已完成", 400_000),
            ])
            messages = scan_kimi.load_conversation(str(wire))

            self.assertEqual([m.role for m in messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(messages[1].text, "第一段结论\n\n第二段结论")  # 同轮文本合并，思考剔除
            self.assertEqual(messages[0].timestamp, 100_000 / 1000)
            self.assertEqual(messages[1].timestamp, 200_000 / 1000)  # 取该轮首个文本分片时间
            self.assertEqual(messages[3].text, "已完成")

    def test_load_conversation_drops_system_origin_user_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wire = Path(td) / "wire.jsonl"
            _write_jsonl(wire, [
                _kimi_user_event("真人提问", 100_000),
                _kimi_user_event("系统注入的事件", 150_000, origin_kind="task-notification"),
                _kimi_assistant_text("回答", 200_000),
            ])
            messages = scan_kimi.load_conversation(str(wire))
            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].text, "真人提问")


class ConversationPreviewTests(unittest.TestCase):
    def test_claude_conversation_keeps_assistant_text_even_before_tool_calls(self) -> None:
        """真实 JSONL 里一次 assistant 轮次的 thinking/text/tool_use 是各自独立的行，且共享同一个
        stop_reason；文本紧跟着工具调用时该行的 stop_reason 也是 tool_use，不代表这段文本不重要
        （历史上按 stop_reason 过滤把这类文本整段丢了，是本用例要回归防止的 bug）。"""
        entries = [
            {"type": "user", "message": {"content": "第一个问题"}},
            {
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "工具调用前的说明"}]},
            },
            {
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
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
                ("assistant", "工具调用前的说明"),
                ("assistant", "第一个最终答复"),
                ("user", "第二个问题"),
                ("assistant", "第二个最终答复"),
            ],
        )

    def test_claude_conversation_drops_task_notification_system_events(self) -> None:
        """Monitor/task-notification 事件在原始记录里也挂在 user 轮次下（`origin.kind` 是
        "task-notification" 而不是 "human"），价值很低，消息历史只保留 Agent 和真人的对话，
        这类系统事件应整条丢弃，不进入返回结果（不是标个 role 展示出来，是完全不展示）。"""
        entries = [
            {
                "type": "user",
                "origin": {"kind": "human"},
                "message": {"content": "真人提问"},
            },
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "message": {"content": "<task-notification>系统事件内容</task-notification>"},
            },
            {
                "type": "user",
                "message": {"content": "/plan 之类无 origin 字段但仍是真人输入"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [
                ("user", "真人提问"),
                ("user", "/plan 之类无 origin 字段但仍是真人输入"),
            ],
        )

    def test_codex_conversation_keeps_commentary_and_removes_task_complete_duplicate(self) -> None:
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
            [("user", "用户问题"), ("assistant", "处理中间状态"), ("assistant", "最终答复")],
        )

    def test_codex_conversation_ignores_null_message_instead_of_literal_none(self) -> None:
        """payload 里的字段即使有 key，值也可能是 JSON null（如任务无输出就结束的
        task_complete）；`payload.get(key, "")` 只在 key 缺失时才用默认值，key 存在但值为
        null 时会拿到 None，`str(None)` 会变成字面量 "None" 混进正文——回归用例覆盖这个坑。"""
        entries = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "问题"}},
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": None},
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual([(message.role, message.text) for message in messages], [("user", "问题")])

    def test_claude_conversation_carries_message_timestamp(self) -> None:
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "第一个问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第一个答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual([message.timestamp for message in messages],
                          [scan_claude._parse_timestamp("2026-07-01T10:00:00Z"),
                           scan_claude._parse_timestamp("2026-07-01T10:00:05Z")])

    def test_claude_legacy_answer_keeps_timestamp_of_original_entry(self) -> None:
        """stop_reason 为 None 的历史遗留格式答复要等下一条用户消息（或文件末尾）才会被
        flush 进 messages，时间戳必须是这条 assistant 记录自己的时间，不是 flush 发生时的时间。"""
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {"stop_reason": None, "content": [{"type": "text", "text": "遗留格式答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(messages[-1].role, "assistant")
        self.assertEqual(messages[-1].timestamp, scan_claude._parse_timestamp("2026-07-01T10:00:05Z"))

    def test_load_conversation_does_not_crash_when_assistant_text_part_is_json_null(self) -> None:
        # content 数组里某个 text part 的 text 字段是 JSON null（key 存在但值为 null）时，
        # 曾在过滤条件 part.get("text", "").strip() 上直接 AttributeError 崩掉整段解析。
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": None}, {"type": "text", "text": "真实答复"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(messages[-1].text, "真实答复")
        self.assertNotIn("None", messages[-1].text)

    def test_codex_conversation_carries_message_timestamp(self) -> None:
        entries = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-01T10:00:00Z",
                "payload": {"type": "user_message", "message": "用户问题"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-01T10:00:03Z",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": "最终答复"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual([message.timestamp for message in messages],
                          [scan_codex._parse_timestamp("2026-07-01T10:00:00Z"),
                           scan_codex._parse_timestamp("2026-07-01T10:00:03Z")])


class BackgroundLuminanceTests(unittest.TestCase):
    """OSC 11 背景色应答 -> 浅/深色判定，供 pickup 自身界面配色跟随外层终端。"""

    def test_dark_background_detected(self) -> None:
        self.assertFalse(pickup._background_is_light(b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07"))

    def test_light_background_detected(self) -> None:
        self.assertTrue(pickup._background_is_light(b"\x1b]11;rgb:ffff/ffff/ffff\x07"))

    def test_two_digit_hex_channels_supported(self) -> None:
        self.assertFalse(pickup._background_is_light(b"\x1b]11;rgb:1e/1e/2e\x07"))

    def test_picks_osc11_not_osc10_foreground(self) -> None:
        mixed = b"\x1b]10;rgb:cccc/cccc/cccc\x07\x1b]11;rgb:1e1e/1e1e/2e2e\x07"
        self.assertFalse(pickup._background_is_light(mixed))

    def test_missing_or_unparsable_report_returns_none(self) -> None:
        self.assertIsNone(pickup._background_is_light(None))
        self.assertIsNone(pickup._background_is_light(b""))
        self.assertIsNone(pickup._background_is_light(b"garbage"))


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
        registry = pickup.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()

        # 启动时无缓存：展示临时兜底标题，并标记为等待后台进程生成（转圈圈）。
        self.assertEqual(store.get_title(session), "这是一条很长的兜底标题")
        self.assertIn(pickup.session_key(session), store.generating)

    def test_low_value_session_never_enters_generating_state(self) -> None:
        session = {
            "source": "claude",
            "id": "greeting",
            "short_id": "greeting",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "在吗",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = pickup.RuntimeRegistry((runtime,))

        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()

        self.assertEqual(store.get_title(session), "在吗")
        self.assertNotIn(pickup.session_key(session), store.generating)

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
        registry = pickup.RuntimeRegistry((claude_runtime,))
        key = pickup.session_key(session)

        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()
        self.assertIn(key, store.generating)

        # 模拟后台进程把标题写进缓存：轮询应拾取它、刷新展示标题并停掉转圈圈。
        fresh_cache = {key: {"fp": titles._fingerprint(session), "title": "后台生成的标题"}}
        with (
            mock.patch.object(pickup.SessionStore, "_cache_file_mtime", return_value=999.0),
            mock.patch.object(pickup.titles, "load_cache", return_value=fresh_cache),
        ):
            store.poll_cache_updates()

        self.assertEqual(store.get_title(session), "后台生成的标题")
        self.assertNotIn(key, store.generating)
        self.assertTrue(store.dirty.is_set())

    def test_failed_title_terminal_state_clears_spinner_and_survives_restart(self) -> None:
        session = {
            "source": "claude",
            "id": "failed",
            "short_id": "failed",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "排查标题生成卡死",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = pickup.RuntimeRegistry((runtime,))
        key = pickup.session_key(session)

        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()
        self.assertIn(key, store.generating)

        failed_cache = {
            key: {
                "fp": titles._fingerprint(session),
                "title": "排查标题生成卡死",
                "generation_state": "failed",
                "generation_version": titles.TITLE_CACHE_VERSION,
            }
        }
        with (
            mock.patch.object(pickup.SessionStore, "_cache_file_mtime", return_value=999.0),
            mock.patch.object(pickup.titles, "load_cache", return_value=failed_cache),
        ):
            store.poll_cache_updates()

        self.assertEqual(store.get_title(session), "排查标题生成卡死")
        self.assertNotIn(key, store.generating)
        self.assertTrue(store.dirty.is_set())

        # 模拟重新启动 pickup：同一缓存版本的失败终态不能再次进入待生成队列。
        with mock.patch.object(pickup.titles, "load_cache", return_value=failed_cache):
            restarted = pickup.SessionStore(limit=20, registry=registry)
            restarted.load()
        self.assertNotIn(key, restarted.generating)
        self.assertFalse(titles.resolve_initial_title(session, failed_cache)[1])

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
        runtime.load_conversation.return_value = [pickup.ConversationMessage("user", "问题")]
        registry = pickup.RuntimeRegistry((runtime,))

        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()

        runtime.load_conversation.assert_not_called()
        self.assertEqual(store.get_conversation(session), [pickup.ConversationMessage("user", "问题")])
        self.assertEqual(store.get_conversation(session), [pickup.ConversationMessage("user", "问题")])
        runtime.load_conversation.assert_called_once_with(session)

    def test_conversation_cache_invalidates_when_history_file_mtime_changes(self) -> None:
        """预览实时刷新和"关闭重开还是旧内容"两个诉求的根因是同一处：缓存必须按历史文件
        mtime 失效，而不是按会话键永久缓存到进程退出。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            session = {
                "source": "claude",
                "id": "abc",
                "short_id": "abc",
                "path": str(path),
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
            runtime.load_conversation.side_effect = [
                [pickup.ConversationMessage("assistant", "旧内容")],
                [pickup.ConversationMessage("assistant", "新内容")],
            ]
            registry = pickup.RuntimeRegistry((runtime,))

            with mock.patch.object(pickup.titles, "load_cache", return_value={}):
                store = pickup.SessionStore(limit=20, registry=registry)
                store.load()

            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            runtime.load_conversation.assert_called_once_with(session)

            # 模拟历史文件被追加写入：mtime 前进，下一次 get_conversation 必须重读，不能沿用旧缓存。
            future = os.stat(path).st_mtime + 5
            os.utime(path, (future, future))

            self.assertEqual(store.get_conversation(session)[0].text, "新内容")
            self.assertEqual(runtime.load_conversation.call_count, 2)

    def test_format_relative_time_thresholds(self) -> None:
        now = 1_000_000.0
        self.assertEqual(pickup._format_relative_time(now - 5, now), "刚刚")
        self.assertEqual(pickup._format_relative_time(now + 100, now), "刚刚")  # 时钟漂移/未来
        self.assertEqual(pickup._format_relative_time(now - 120, now), "2分钟前")
        self.assertEqual(pickup._format_relative_time(now - 3 * 3600, now), "3小时前")
        # 超过一天退回绝对日期时间（沿用 MM-DD HH:MM）
        old = now - 3 * 86400
        self.assertEqual(
            pickup._format_relative_time(old, now),
            pickup.datetime.fromtimestamp(old).strftime("%m-%d %H:%M"),
        )

    def test_fit_cell_uses_terminal_display_width(self) -> None:
        self.assertEqual(pickup._text_width("标题"), 4)
        self.assertEqual(pickup._fit_cell("标题", 6), "标题  ")
        self.assertEqual(pickup._fit_cell("标题很长", 5), "标题 ")
        self.assertEqual(pickup._text_width(pickup._fit_cell("✅完成", 8)), 8)

    def test_preview_renders_messages_as_chronological_chat(self) -> None:
        messages = [
            pickup.ConversationMessage("user", "请分析启动速度"),
            pickup.ConversationMessage("assistant", "主要耗时来自历史扫描"),
            pickup.ConversationMessage("user", "再增加聊天记录预览"),
            pickup.ConversationMessage("assistant", "已经完成实现和验证"),
        ]

        lines = pickup._preview_lines(messages, "Codex", 16)
        text_lines = [line for _, line, _ in lines]

        self.assertEqual(text_lines[0], "● 你")
        self.assertIn("◆ Codex", text_lines)
        self.assertEqual(text_lines.count("● 你"), 2)
        self.assertEqual(text_lines.count("◆ Codex"), 2)
        self.assertTrue(all(pickup._text_width(line) <= 16 for line in text_lines))

    def test_preview_lines_show_timestamp_suffix_only_when_available(self) -> None:
        ts = 1_780_000_000.0
        messages = [
            pickup.ConversationMessage("user", "带时间戳的消息", ts),
            pickup.ConversationMessage("assistant", "老格式缺时间戳的消息"),
        ]

        lines = pickup._preview_lines(messages, "Claude", 40)
        role_lines = [(kind, line, suffix) for kind, line, suffix in lines if kind in ("user", "assistant")]

        self.assertEqual(role_lines[0][1], "● 你")
        self.assertIn(pickup.format_message_time(ts), role_lines[0][2])
        self.assertEqual(role_lines[1][1], "◆ Claude")
        self.assertEqual(role_lines[1][2], "")

    def test_all_sessions_keep_stable_order_and_prepend_new_on_refresh(self) -> None:
        """列表展示出来后已有会话位置固定：内容更新（mtime 变新）不再跳到顶上；
        后台重扫只把新出现的会话按 mtime 倒序插到最前。"""
        def make_session(session_id: str, mtime: float) -> dict:
            return {
                "source": "claude",
                "id": session_id,
                "short_id": session_id,
                "mtime": mtime,
                "size_bytes": 1,
                "size_kb": 1,
                "native_title": None,
                "fallback_title": f"会话{session_id}",
            }

        first = [make_session("a", 3), make_session("b", 2), make_session("c", 1)]
        # 第二轮：b 有了新消息（mtime 顶到最新），d 是全新会话
        second = [make_session("b", 100), make_session("d", 50), make_session("a", 3), make_session("c", 1)]
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_signature.return_value = None
        claude_runtime.scan_sessions.side_effect = [first, second]
        registry = pickup.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()
            self.assertEqual([s["id"] for s in store.all_sessions()], ["a", "b", "c"])

            changed = store.refresh()

        self.assertTrue(changed)  # 新会话 d 出现，集合确实变了
        # d 插到最前；a/b/c 保持首次展示时的相对位置，b 内容更新但不移动
        self.assertEqual([s["id"] for s in store.all_sessions()], ["d", "a", "b", "c"])
        self.assertEqual(store.all_sessions()[2]["mtime"], 100)

class NavStub:
    """`_new_session_cwd` 只需要 `project_key` 属性；界面层真实用的是
    `ui.nav.NavState`，测试这里用一个最小 stub 避免依赖 ui 包。"""

    def __init__(self, project_key: str | None = None) -> None:
        self.project_key = project_key


class ProjectSidebarTests(unittest.TestCase):
    def test_normalize_cwd_trailing_slash_and_empty(self) -> None:
        self.assertEqual(pickup._normalize_cwd("/a/b/"), "/a/b")
        self.assertEqual(pickup._normalize_cwd(""), "")
        self.assertEqual(pickup._normalize_cwd(None), "")
        self.assertEqual(pickup._normalize_cwd("/"), "")

    def test_project_groups_sorted_by_count_then_latest_mtime(self) -> None:
        sessions_by_source = {
            "claude": [
                {"cwd": "/a/x", "mtime": 10},
                {"cwd": "/a/x", "mtime": 5},
                {"cwd": "/a/y", "mtime": 20},
                {"cwd": "/a/z", "mtime": 1},
            ],
        }
        groups = pickup._project_groups(sessions_by_source)
        keys = [g["cwd_key"] for g in groups]

        # 会话数最多的项目排第一；会话数相同的项目按最近会话时间倒序。
        self.assertEqual(keys, ["/a/x", "/a/y", "/a/z"])
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["latest_mtime"], 10)

    def test_project_groups_merges_all_sources(self) -> None:
        sessions_by_source = {
            "claude": [{"cwd": "/a/x", "mtime": 1}],
            "codex": [{"cwd": "/a/x", "mtime": 2}],
        }
        groups = pickup._project_groups(sessions_by_source)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["latest_mtime"], 2)

    def test_project_groups_labels_empty_and_root_cwd_as_unknown(self) -> None:
        sessions_by_source = {"claude": [{"cwd": "", "mtime": 1}, {"cwd": "/", "mtime": 2}]}
        groups = pickup._project_groups(sessions_by_source)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["cwd_key"], "")
        self.assertEqual(groups[0]["label"], pickup.UNKNOWN_PROJECT_LABEL)
        self.assertEqual(groups[0]["count"], 2)

    def test_disambiguate_adds_parent_only_on_conflict(self) -> None:
        labels = pickup._disambiguate_labels(["/a/x/cli", "/b/y/app"])
        self.assertEqual(labels["/a/x/cli"], "cli")
        self.assertEqual(labels["/b/y/app"], "app")

    def test_disambiguate_climbs_multiple_levels(self) -> None:
        # 两个同名 cli 目录连上一级目录名也相同，需要继续向上爬升才能唯一区分。
        labels = pickup._disambiguate_labels(["/a/p/cli", "/b/p/cli"])
        self.assertEqual(labels["/a/p/cli"], "a/p/cli")
        self.assertEqual(labels["/b/p/cli"], "b/p/cli")
        self.assertNotEqual(labels["/a/p/cli"], labels["/b/p/cli"])

    def test_filter_sessions_uses_exact_cwd_not_basename(self) -> None:
        sessions = [
            {"cwd": "/a/x/cli", "mtime": 1},
            {"cwd": "/b/y/cli", "mtime": 2},
        ]
        filtered = pickup._filter_sessions(sessions, "/a/x/cli")

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["cwd"], "/a/x/cli")

    def test_filter_sessions_none_key_returns_unfiltered(self) -> None:
        sessions = [{"cwd": "/a/x", "mtime": 1}]
        self.assertEqual(pickup._filter_sessions(sessions, None), sessions)

    def test_new_session_cwd_prefers_selected_project_over_session(self) -> None:
        store = mock.Mock()
        nav = NavStub(project_key="/proj/a")
        session = {"cwd": "/proj/other"}

        self.assertEqual(pickup._new_session_cwd(store, nav, session), "/proj/a")

    def test_new_session_cwd_falls_back_to_session_cwd_when_all_projects(self) -> None:
        store = mock.Mock()
        nav = NavStub()
        session = {"cwd": "/proj/session"}

        self.assertEqual(pickup._new_session_cwd(store, nav, session), "/proj/session")

    def test_new_session_cwd_none_without_project_or_session(self) -> None:
        store = mock.Mock()
        nav = NavStub()

        self.assertIsNone(pickup._new_session_cwd(store, nav, None))

    def test_new_session_cwd_unknown_project_returns_none(self) -> None:
        store = mock.Mock()
        nav = NavStub(project_key="")

        self.assertIsNone(pickup._new_session_cwd(store, nav, {"cwd": "/should/not/use"}))


class TmuxRequirementTests(unittest.TestCase):
    def test_supported_version_passes(self) -> None:
        with (
            mock.patch.object(pickup.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(pickup.embed, "_tmux_version", return_value=(3, 2)),
        ):
            pickup._require_tmux()

    def test_unparseable_version_does_not_block(self) -> None:
        with (
            mock.patch.object(pickup.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(pickup.embed, "_tmux_version", return_value=None),
        ):
            pickup._require_tmux()

    def test_old_version_reports_required_and_current_versions(self) -> None:
        with (
            mock.patch.object(pickup.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(pickup.embed, "_tmux_version", return_value=(3, 1)),
            mock.patch("builtins.print") as print_mock,
            self.assertRaises(SystemExit) as ctx,
        ):
            pickup._require_tmux()

        self.assertEqual(ctx.exception.code, 1)
        output = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("3.2", output)
        self.assertIn("3.1", output)


class DirectLaunchTests(unittest.TestCase):
    """`pickup claude [参数…]` / `pickup codex [参数…]` 直启透传子命令的分发逻辑。"""

    def _registry_returning(self, plan: LaunchPlan) -> mock.Mock:
        registry = mock.Mock()
        registry.build_passthrough_plan.return_value = plan
        return registry

    def test_passes_through_args_and_wraps_with_keepalive_by_default(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "把测试修到全绿"), None)
        wrapped = LaunchPlan(("tmux", "-L", "pickup-keepalive", "new-session", "-A", "-s", "sc-claude-xxxx"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
        ):
            keepalive_mock.enabled.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"
            keepalive_mock.wrap_plan.return_value = wrapped

            pickup._dispatch_direct_launch(["claude", "把测试修到全绿"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["把测试修到全绿"])
        keepalive_mock.enabled.assert_called_once_with(False)
        keepalive_mock.wrap_plan.assert_called_once_with(plan, "claude", "xxxx")
        execute_launch.assert_called_once_with(wrapped)

    def test_no_keepalive_prefix_strips_flag_and_skips_wrap(self) -> None:
        plan = LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox", "resume"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch") as execute_launch,
        ):
            keepalive_mock.enabled.return_value = False

            pickup._dispatch_direct_launch(["--no-keepalive", "codex", "resume"], registry)

        registry.build_passthrough_plan.assert_called_once_with("codex", ["resume"])
        keepalive_mock.enabled.assert_called_once_with(True)
        keepalive_mock.wrap_plan.assert_not_called()
        execute_launch.assert_called_once_with(plan)

    def test_launch_error_prints_message_and_exits_nonzero(self) -> None:
        plan = LaunchPlan(("claude",), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup, "execute_launch", side_effect=pickup.LaunchError("未找到 claude 命令")),
        ):
            keepalive_mock.enabled.return_value = False

            with self.assertRaises(SystemExit) as ctx:
                pickup._dispatch_direct_launch(["claude"], registry)

        self.assertEqual(ctx.exception.code, 1)

    def test_tty_with_embed_enters_tui_with_direct_launch_plan(self) -> None:
        """真实终端 + 内嵌可用：直启进入 TUI 侧边栏模式，启动计划交给 Textual
        主屏托管，不再走 execvp 全屏接管。"""
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(pickup.sys, "stdin") as stdin_mock,
            mock.patch.object(pickup.sys, "stdout") as stdout_mock,
            mock.patch.object(pickup, "keepalive") as keepalive_mock,
            mock.patch.object(pickup.embed, "available", return_value=True),
            mock.patch.object(pickup, "SessionStore") as store_cls,
            mock.patch.object(pickup, "_spawn_title_daemon"),
            mock.patch.object(pickup, "_probe_osc_colours", return_value=None),
            mock.patch("ui.app.run_app", return_value=None) as run_app,
            mock.patch.object(pickup.embed, "close_channel"),
            mock.patch.object(pickup, "execute_launch") as execute_launch,
        ):
            stdin_mock.isatty.return_value = True
            stdout_mock.isatty.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"

            pickup._dispatch_direct_launch(["claude"], registry)

        store_cls.return_value.load.assert_called_once_with()
        run_app.assert_called_once()
        args = run_app.call_args.args
        self.assertIs(args[0], store_cls.return_value)
        self.assertIs(args[1], True)
        self.assertEqual(args[2], pickup._DirectLaunch(plan, "claude", "xxxx"))
        execute_launch.assert_not_called()
        keepalive_mock.wrap_plan.assert_not_called()


class HandoffDigestTests(unittest.TestCase):
    """跨运行时接手提示词里的对话摘录：标题只有十几个字，摘录是目标 agent 的任务锚点。"""

    class _StubRuntime(BaseRuntime):
        id = "claude"
        display_name = "Claude"
        executable = "claude"
        history_reading_hint = "格式提示"

        def __init__(self, messages) -> None:
            self._messages = messages

        def scan_sessions(self, limit):
            return []

        def load_conversation(self, session):
            if isinstance(self._messages, Exception):
                raise self._messages
            return self._messages

        def build_resume_plan(self, session):
            raise NotImplementedError

        def build_new_plan(self, handoff):
            raise NotImplementedError

        def build_new_session_plan(self, cwd):
            raise NotImplementedError

    def _export(self, messages, **session_extra):
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as history:
            session = {
                "path": history.name,
                "cwd": "/tmp",
                "status_tag": titles.STATUS_PENDING,
                "first_user_msg": "",
                "last_user_msg": "",
                "last_agent_msg": "",
            }
            session.update(session_extra)
            return self._StubRuntime(messages).export_handoff(session, "标题")

    def test_digest_keeps_first_need_and_last_eight_messages_with_llm_facing_roles(self) -> None:
        # 角色必须标"用户"而不是"你"：摘录是给接手的大模型看的，"你"会被误解为指它自己。
        messages = [ConversationMessage("user", "最初的需求说明")]
        for i in range(10):
            role = "assistant" if i % 2 == 0 else "user"
            messages.append(ConversationMessage(role, f"消息{i}" + ("长" * 500 if i == 9 else "")))

        handoff = self._export(messages)

        lines = handoff.conversation_digest.splitlines()
        self.assertEqual(lines[0], "【原始需求】最初的需求说明")
        self.assertEqual(lines[1], "【最近对话】")
        self.assertEqual(len(lines), 2 + 8)  # 最近 8 条
        self.assertTrue(lines[2].startswith(("用户: ", "助手: ")))
        self.assertNotIn("你:", handoff.conversation_digest)
        self.assertTrue(lines[-1].endswith("…"))  # 超长消息被截断

    def test_digest_skips_first_need_when_already_in_recent_window(self) -> None:
        messages = [
            ConversationMessage("user", "问题"),
            ConversationMessage("assistant", "答复"),
        ]
        handoff = self._export(messages)
        self.assertNotIn("【原始需求】", handoff.conversation_digest)
        self.assertEqual(
            handoff.conversation_digest, "【最近对话】\n用户: 问题\n助手: 答复"
        )

    def test_digest_falls_back_to_scan_fields_when_conversation_unavailable(self) -> None:
        # 对话提取失败（异常）时静默降级到扫描层首尾消息，不阻断接力。
        handoff = self._export(
            OSError("boom"),
            first_user_msg="最初需求",
            last_user_msg="最后追问",
            last_agent_msg="最后答复",
        )
        self.assertEqual(
            handoff.conversation_digest,
            "【原始需求】最初需求\n【最近对话】\n用户: 最后追问\n助手: 最后答复",
        )

    def test_prompt_without_digest_or_status_matches_legacy_shape(self) -> None:
        handoff = self._export([], status_tag="")
        self.assertEqual(handoff.conversation_digest, "")
        prompt = handoff.render_prompt()
        self.assertNotIn("对话摘录", prompt)
        self.assertNotIn("会话状态", prompt)
        self.assertIn("请先读取上述会话历史", prompt)

    def test_prompt_with_digest_omits_status_and_keeps_authority_note(self) -> None:
        messages = [
            ConversationMessage("user", "问题"),
            ConversationMessage("assistant", "答复"),
        ]
        prompt = self._export(messages).render_prompt()
        # 接力目的就是接着往下干，绝不能把源会话状态（尤其"已完成"）注入提示词误导接手方。
        self.assertNotIn("会话状态", prompt)
        self.assertIn("以下是从原会话自动提取的对话摘录", prompt)
        self.assertIn("摘录与文件不一致时以文件为准", prompt)
        self.assertIn("请以上述摘录为线索读取原会话历史", prompt)
        self.assertIn("用户: 问题", prompt)

    def test_build_new_plan_prompt_carries_digest_for_both_runtimes(self) -> None:
        handoff = Handoff(
            source_runtime_id="claude", source_runtime_name="Claude", title="标题",
            history_path="/tmp/h.jsonl", original_cwd="/tmp", history_reading_hint="hint",
            conversation_digest="【最近对话】\n用户: 独特摘录内容",
        )
        for runtime in (ClaudeRuntime(), CodexRuntime()):
            plan = runtime.build_new_plan(handoff)
            self.assertIn("独特摘录内容", plan.argv[-1])


class AgentApiTests(unittest.TestCase):
    def _session(
        self, sid: str, title: str, mtime: int, *, cwd: str = "/tmp/demo", status=titles.STATUS_DONE,
        live: bool = False, pid: int | None = None, last_user_msg: str = "", last_agent_msg: str = "",
    ) -> dict:
        return {
            "source": "claude",
            "id": sid,
            "short_id": sid[:8],
            "cwd": cwd,
            "cwd_display": cwd,
            "mtime": mtime,
            "display_time": "07-01 12:00",
            "size_bytes": 1000,
            "size_kb": 1.0,
            "native_title": None,
            "fallback_title": title,
            "status_tag": status,
            "live": live,
            "pid": pid,
            "first_user_msg": title,
            "last_user_msg": last_user_msg,
            "last_agent_msg": last_agent_msg,
            "path": f"/tmp/{sid}.jsonl",
        }

    def _registry(self, sessions: list[dict], messages: list[ConversationMessage] | None = None):
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = sessions
        runtime.load_conversation.return_value = messages or []
        runtime.build_resume_plan.side_effect = (
            lambda session: LaunchPlan(argv=("claude", "--resume", session["id"]), cwd=session.get("cwd"))
        )
        runtime.export_handoff.side_effect = lambda session, title: Handoff(
            source_runtime_id="claude", source_runtime_name="Claude", title=title,
            history_path=session.get("path") or "", original_cwd=session.get("cwd") or "",
            history_reading_hint="hint",
        )

        class FakeRegistry:
            def __iter__(self):
                return iter([runtime])

            def get(self, runtime_id: str):
                if runtime_id != "claude":
                    raise AssertionError(runtime_id)
                return runtime

        return FakeRegistry(), runtime

    def test_list_top_is_result_limit_and_compact_fields_include_resume(self) -> None:
        # --limit 是扫描深度，--top 才是返回条数上限；compact 默认只保留 Agent 常用字段。
        sessions = [
            self._session("aaa11111", "第一个", 30),
            self._session("bbb22222", "第二个", 20),
            self._session("ccc33333", "第三个", 10),
        ]
        registry, runtime = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=3, top=2, compact=True, status=None, cwd=None, fields=None)

        result = agent_api.cmd_list(args, registry)

        runtime.scan_sessions.assert_called_once_with(3)
        self.assertEqual(result["data"]["count"], 2)
        first = result["data"]["sessions"][0]
        self.assertEqual(first["id"], "aaa11111")
        self.assertEqual(first["resume_command"], "claude --resume aaa11111")
        self.assertTrue(first["resumable"])
        self.assertNotIn("history_path", first)

    def test_search_scores_and_sorts_by_relevance_before_time(self) -> None:
        # 标题命中比分散字段命中权重更高；排序先看相关性，再按更新时间。
        sessions = [
            self._session("oldtitle", "fable 数据修复", 10),
            self._session("newcwdxx", "普通问题", 999, cwd="/tmp/fable-project"),
            self._session("nomatchx", "普通问题", 1000),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(
            keywords=["fable"], deep=False, runtime=None, limit=10, top=2, compact=True, fields=None,
        )

        result = agent_api.cmd_search(args, registry)

        found = result["data"]["sessions"]
        self.assertEqual([item["id"] for item in found], ["oldtitle", "newcwdxx"])
        self.assertGreater(found[0]["score"], found[1]["score"])
        self.assertEqual(found[0]["matched_via"], "quick")
        self.assertEqual(found[0]["matched_fields"], ["title", "fallback_title", "first_user_msg"])
        self.assertIn("resume_command", found[0])

    def test_show_out_writes_full_result_and_stdout_returns_reference(self) -> None:
        session = self._session("show1234", "查看完整会话", 1)
        messages = [
            ConversationMessage("user", "问题一"),
            ConversationMessage("assistant", "答复一"),
            ConversationMessage("user", "问题二"),
        ]
        registry, _ = self._registry([session], messages)
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "show.json")
            args = mock.Mock(session="claude:show1234", limit=10, full=True, messages=None, out=out_path,
                              compact=True, fields=None)

            result = agent_api.cmd_show(args, registry)

            self.assertTrue(result["data"]["messages_omitted"])
            self.assertEqual(result["data"]["output_path"], out_path)
            self.assertNotIn("messages", result["data"])
            saved = json.loads(Path(out_path).read_text(encoding="utf-8"))

        self.assertTrue(saved["ok"])
        self.assertEqual(saved["data"]["message_count_shown"], 3)
        self.assertEqual(saved["data"]["messages"][2]["text"], "问题二")

    def test_session_payload_exposes_live_pid_and_trims_summary(self) -> None:
        # 管家 Agent 场景：list/search 默认要能看出「哪个 CodingAgent 在跑」（live/pid）
        # 和「最近聊了什么」（last_user/last_agent，硬截断精简，不是全文）。
        long_text = "问" * 200
        session = self._session(
            "live1234", "运行中的会话", 1, live=True, pid=12345,
            last_user_msg=long_text, last_agent_msg="好的，正在处理",
        )
        payload = agent_api.session_payload(session, {}, runtime=None)

        self.assertTrue(payload["live"])
        self.assertEqual(payload["pid"], 12345)
        self.assertEqual(payload["last_agent"], "好的，正在处理")
        self.assertTrue(payload["last_user"].endswith("…"))
        self.assertLessEqual(len(payload["last_user"]), agent_api._SUMMARY_TRIM_LEN + 1)

        idle_session = self._session("idle5678", "已结束的会话", 1, live=False)
        idle_payload = agent_api.session_payload(idle_session, {}, runtime=None)
        self.assertFalse(idle_payload["live"])
        self.assertIsNone(idle_payload["pid"])

    def test_session_payload_exposes_keepalive_flag(self) -> None:
        session = self._session("keep1234", "保活中的会话", 1, live=True, pid=12345)
        session["keepalive_name"] = "sc-claude-keep1234"
        payload = agent_api.session_payload(session, {}, runtime=None)
        self.assertTrue(payload["keepalive"])

        no_keepalive_session = self._session("plain1234", "普通会话", 1)
        plain_payload = agent_api.session_payload(no_keepalive_session, {}, runtime=None)
        self.assertFalse(plain_payload["keepalive"])

    def test_list_and_search_annotate_scanned_sessions_before_building_payload(self) -> None:
        # cmd_list/cmd_search 必须先对本次扫描出的会话跑一次 keepalive.annotate，
        # 才能把「是否在后台保活」正确反映进输出字段，不能让调用方自己再查一遍。
        sessions = [self._session("live1234", "在跑的会话", 1, live=True, pid=555)]
        registry, _ = self._registry(sessions)

        def _fake_annotate(scanned_sessions):
            for item in scanned_sessions:
                if item.get("pid") == 555:
                    item["keepalive_name"] = "sc-claude-live1234"

        with mock.patch.object(agent_api.keepalive, "annotate", side_effect=_fake_annotate) as mocked:
            list_args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None,
                                   fields=None, live=None)
            list_result = agent_api.cmd_list(list_args, registry)
            self.assertTrue(mocked.called)
            self.assertTrue(list_result["data"]["sessions"][0]["keepalive"])

        with mock.patch.object(agent_api.keepalive, "annotate", side_effect=_fake_annotate):
            search_args = mock.Mock(keywords=["在跑"], deep=False, runtime=None, limit=10, top=None,
                                     compact=True, fields=None)
            search_result = agent_api.cmd_search(search_args, registry)
            self.assertTrue(search_result["data"]["sessions"][0]["keepalive"])

    def test_list_live_filter_keeps_only_running_sessions(self) -> None:
        sessions = [
            self._session("run11111", "跑着的", 20, live=True, pid=111),
            self._session("done2222", "结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None, fields=None, live=True)

        result = agent_api.cmd_list(args, registry)

        self.assertEqual([s["id"] for s in result["data"]["sessions"]], ["run11111"])

    def test_list_without_live_flag_still_returns_all_sessions(self) -> None:
        # 回归：mock.Mock() 未显式设置的属性会自动生成一个真值 Mock，--live 判断必须
        # 用 `is True` 而不是单纯 truthy，否则老调用方（未传 live 参数）会被误过滤。
        sessions = [
            self._session("run11111", "跑着的", 20, live=True, pid=111),
            self._session("done2222", "结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None, fields=None)

        result = agent_api.cmd_list(args, registry)

        self.assertEqual(len(result["data"]["sessions"]), 2)

    def test_search_live_filter_keeps_only_running_sessions(self) -> None:
        sessions = [
            self._session("run11111", "fable 跑着的", 20, live=True, pid=111),
            self._session("done2222", "fable 结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(
            keywords=["fable"], deep=False, runtime=None, limit=10, top=None, compact=True, fields=None, live=True,
        )

        result = agent_api.cmd_search(args, registry)

        self.assertEqual([s["id"] for s in result["data"]["sessions"]], ["run11111"])

    def test_context_reports_live_and_pid(self) -> None:
        session = self._session("ctxsess1", "上下文会话", 1, live=True, pid=999)
        registry, _ = self._registry([session])
        args = mock.Mock(session="claude:ctxsess1", limit=10)

        result = agent_api.cmd_context(args, registry)

        self.assertTrue(result["data"]["live"])
        self.assertEqual(result["data"]["pid"], 999)

    def test_describe_list_and_search_document_live_flag_and_new_fields(self) -> None:
        # pickup describe 的输出与实现同源，改完命令必须同步这里，防止参数/字段说明漂移。
        args = mock.Mock(target="list")
        result = agent_api.cmd_describe(args, registry=None)
        list_flags = [flag for arg in result["data"]["args"] for flag in arg["flags"]]
        self.assertIn("--live", list_flags)
        self.assertIn("live", result["data"]["fields"])
        self.assertIn("keepalive", result["data"]["fields"])
        self.assertIn("pid", result["data"]["fields"])
        self.assertIn("last_user", result["data"]["fields"])
        self.assertIn("last_agent", result["data"]["fields"])

        args = mock.Mock(target="search")
        result = agent_api.cmd_describe(args, registry=None)
        search_flags = [flag for arg in result["data"]["args"] for flag in arg["flags"]]
        self.assertIn("--live", search_flags)


class StartupLatencyTests(unittest.TestCase):
    """首屏延迟测量：改动扫描/界面/标题相关代码后必须跑这个用例并如实汇报耗时。

    见 AGENTS.md「验证要求」：pickup 首屏（启动到首次渲染）延迟目标 ≤1s，
    界面层改用 Textual 后这条已从硬性红线放宽为非阻断项——但仍要求实测并
    汇报数值，不能不测。这里只做粗粒度的灾难性回归防护（>5s 才真正判定失败，
    比如不小心引入了一次同步网络调用），1s 目标本身只打印不作为断言依据：
    共享机器上跑其他重负载任务时，真实扫描性能会被拖到 1s 以上而与代码质量
    无关（真实测过：mongodump 之类的重 I/O 任务在跑时，本机 load average 到
    8+，同一次扫描耗时能从 <1s 抖到 1.7s+）。
    registry.scan_all() 就是 main() 里 store.load() 实际同步阻塞首屏的调用；
    本机无真实会话数据时（例如 CI/新机）跳过，避免假失败。
    """

    def test_scan_all_first_screen_latency(self) -> None:
        from runtime import default_registry

        has_data = os.path.isdir(scan_claude.PROJECTS_DIR) or bool(scan_codex._find_all_session_files())
        if not has_data:
            self.skipTest("本机无真实会话数据，首屏延迟测量跳过")

        registry = default_registry()
        t0 = time.perf_counter()
        registry.scan_all(50)
        elapsed = time.perf_counter() - t0
        print(f"\n[首屏延迟] registry.scan_all(50) 耗时 {elapsed * 1000:.0f}ms"
              f"（目标 ≤1000ms，非阻断项；共享机器负载高时会自然超出）")

        self.assertLess(
            elapsed, 5.0,
            f"registry.scan_all(50) 耗时 {elapsed * 1000:.0f}ms，"
            f"远超 1s 目标的合理误差范围，需要排查是否引入了灾难性性能回归"
            f"（而不是机器负载波动）",
        )


if __name__ == "__main__":
    unittest.main()
