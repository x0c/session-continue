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
import agent_api
import titles
from models import ConversationMessage, LaunchPlan


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

    def test_live_session_ids_parses_uuid_from_lsof_rollout_line(self) -> None:
        # 状态列判活：codex 进程持有自己的 rollout jsonl（写模式），从 lsof
        # 输出的文件名里抽出会话 UUID 即视为存活。
        uuid = "019f2c27-c9b0-7dc3-a600-8678bf0e8dcc"
        lsof_output = (
            f"codex     47372 geraltgraham   45w      REG     1,17  468333  651218417 "
            f"/Users/geraltgraham/.codex/sessions/2026/07/04/"
            f"rollout-2026-07-04T16-03-52-{uuid}.jsonl\n"
        )

        def fake_check_output(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return b"47372\n"
            if cmd[0] == "lsof":
                return lsof_output.encode()
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch("scan_codex.subprocess.check_output", side_effect=fake_check_output):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {uuid})

    def test_live_session_ids_returns_empty_when_pgrep_unavailable(self) -> None:
        # pgrep 缺失或调用失败时静默降级为空集，不抛异常。
        with mock.patch(
            "scan_codex.subprocess.check_output", side_effect=FileNotFoundError()
        ):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, set())


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

    def test_preview_uses_full_terminal_and_clears_before_returning(self) -> None:
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 80)
        messages = [sc.ConversationMessage("user", "问题")]

        with mock.patch.object(sc.curses, "color_pair", return_value=0):
            sc._draw_preview(screen, messages, "标题", "Claude", 0)

        screen.erase.assert_called_once_with()
        positions = {(call.args[0], call.args[1]) for call in screen.addnstr.call_args_list}
        self.assertIn((0, 0), positions)
        self.assertIn((23, 0), positions)

        store = mock.Mock()
        store.get_conversation.return_value = messages
        store.registry.get.return_value.display_name = "Claude"
        session = {"source": "claude", "id": "abc"}
        screen.getch.return_value = ord("q")
        with mock.patch.object(sc, "_draw_preview", return_value=0):
            should_resume = sc._show_preview(screen, store, session, "标题")
        self.assertFalse(should_resume)
        screen.clear.assert_called_once_with()

        screen.clear.reset_mock()
        screen.getch.return_value = 10
        with mock.patch.object(sc, "_draw_preview", return_value=0):
            should_resume = sc._show_preview(screen, store, session, "标题")
        self.assertTrue(should_resume)
        screen.clear.assert_called_once_with()

    def test_directory_column_gets_more_space_on_normal_terminals(self) -> None:
        col_num, col_title, col_dir, col_time, col_size, col_status = sc._column_widths(120)

        self.assertEqual((col_num, col_time, col_size, col_status), (4, 17, 11, 10))
        self.assertGreaterEqual(col_title, 10)
        self.assertGreaterEqual(col_dir, 30)
        self.assertEqual(
            sum((col_num, col_title, col_dir, col_time, col_size, col_status)) + len(sc.COL_GAP) * 5,
            119,
        )


class AgentApiTests(unittest.TestCase):
    def _session(self, sid: str, title: str, mtime: int, *, cwd: str = "/tmp/demo", status=titles.STATUS_DONE) -> dict:
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
            "live": False,
            "first_user_msg": title,
            "last_user_msg": "",
            "last_agent_msg": "",
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
            args = mock.Mock(session="claude:show1234", limit=10, full=True, messages=None, out=out_path, compact=True)

            result = agent_api.cmd_show(args, registry)

            self.assertTrue(result["data"]["messages_omitted"])
            self.assertEqual(result["data"]["output_path"], out_path)
            self.assertNotIn("messages", result["data"])
            saved = json.loads(Path(out_path).read_text(encoding="utf-8"))

        self.assertTrue(saved["ok"])
        self.assertEqual(saved["data"]["message_count_shown"], 3)
        self.assertEqual(saved["data"]["messages"][2]["text"], "问题二")


class StartupLatencyTests(unittest.TestCase):
    """首屏延迟硬性上限闸门：改动扫描/界面/标题相关代码后必须跑这个用例。

    见 AGENTS.md「验证要求」：sc 首屏（启动到首次渲染）延迟必须 ≤1s。
    registry.scan_all() 就是 main() 里 store.load() 实际同步阻塞首屏的调用；
    本机无真实会话数据时（例如 CI/新机）跳过，避免假失败。
    """

    def test_scan_all_first_screen_under_one_second(self) -> None:
        from runtime import default_registry

        has_data = os.path.isdir(scan_claude.PROJECTS_DIR) or bool(scan_codex._find_all_session_files())
        if not has_data:
            self.skipTest("本机无真实会话数据，首屏延迟闸门跳过")

        registry = default_registry()
        t0 = time.perf_counter()
        registry.scan_all(50)
        elapsed = time.perf_counter() - t0

        self.assertLess(
            elapsed, 1.0, f"registry.scan_all(50) 耗时 {elapsed * 1000:.0f}ms，超过首屏 1s 硬性上限"
        )


if __name__ == "__main__":
    unittest.main()
