"""ui/ 包（Textual 界面层）的 Pilot 交互测试。

取代旧版 test_session_scanning.py 里那些直接 mock curses/stdscr 的界面测试
（_run/_draw*/鼠标 SGR 解析等，随 curses 一起删除）。这里用 Textual 官方支持的
无终端 Pilot（App.run_test()）驱动真实的 App/Screen/Widget 事件循环，覆盖会话
导航、项目筛选、启动/内嵌流程、预览页、各类弹窗。

内嵌面板（EmbedPane）对 tmux 的依赖在这里用真实 tmux 会话验证（而不是 mock
embed.* 调用）：项目已有的 embed.py 单测负责纯函数层，selftest.sh 负责完整
真机冒烟；这里介于两者之间，用真实但轻量的 tmux 会话验证 MainScreen ↔ EmbedPane
↔ embed.py 的接线是否正确（这条接线正是本次从 curses 迁移到 Textual 的核心）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from pickup import i18n

# 界面测试固定英文，避免 CI/本机 LANG=zh* 时断言漂移
i18n.set_lang("en")

import pickup
from pickup.models import LaunchPlan
from textual import events
from textual.color import Color
from textual.geometry import Offset, Size
from textual.widgets import Footer, Input, ListItem
from pickup.ui.app import PickupApp
from pickup.ui.embed_pane import EmbedPane
from pickup.ui.split_pane_area import SplitPaneArea
from pickup.ui.modals import ConfirmModal, PickMenuModal, RuntimePickerModal
from pickup.ui.session_list import NEW_SESSION_ID, SessionCard, SessionListView

HAS_TMUX = shutil.which("tmux") is not None


def _primary_embed_pane(screen) -> EmbedPane:
    """MainScreen 多分屏右栏里取第一个 EmbedPane（单格测试沿用此入口）。"""
    area = screen.query_one(SplitPaneArea)
    for cell in area._cells():  # noqa: SLF001
        pane = cell.embed_pane()
        if pane is not None:
            return pane
    raise AssertionError("没有可用的内嵌面板")


async def _wait_for_embed_pane(screen) -> EmbedPane:
    """等 SplitPaneArea 异步挂载完成后再取 EmbedPane。"""

    def _ready() -> bool:
        area = screen.query_one(SplitPaneArea)
        for cell in area._cells():  # noqa: SLF001
            if cell.embed_pane() is not None:
                return True
        return False

    await _wait_until(_ready)
    return _primary_embed_pane(screen)


async def _wait_for_embed_session(
    screen, session_name: str, *, tries: int = 500, interval: float = 0.01,
) -> EmbedPane:
    """右栏异步替换格子时反复取当前 Widget，直到它已绑定目标托管会话。"""
    for _ in range(tries):
        try:
            pane = _primary_embed_pane(screen)
        except AssertionError:
            pane = None
        if pane is not None and pane.session_name == session_name:
            return pane
        await asyncio.sleep(interval)
    raise AssertionError(
        f"等待 {tries * interval:.2f}s 后仍未挂载托管会话：{session_name}"
    )



async def _wait_until(predicate, *, tries: int = 200, interval: float = 0.01) -> None:
    """等待后台 worker 达到断言条件，避免用固定长延迟放慢整套界面测试。"""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.2f}s 后条件仍未满足")


async def _wait_for_pane_text(pane, text: str, *, tries: int = 60, interval: float = 0.1) -> None:
    """抓帧现在跑在后台线程（见 embed_pane.py 的性能修复：滚轮/输入处理不再
    同步调用 embed.capture，避免卡住主线程），测试里不能再直接同步调用一个
    "_capture_now" 方法强制抓一帧，改成轮询等待后台线程把新画面渲染出来。"""
    import asyncio

    for _ in range(tries):
        if pane._grid is not None and text in pane.render().plain:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.1f}s 后仍未看到文本：{text!r}；"
                          f"当前画面：{pane.render().plain!r}")


async def _wait_for_session_name(pane, *, tries: int = 60, interval: float = 0.1) -> None:
    """等待 MainScreen 的托管 worker 完成。`embed.host_session`（真正的阻塞 tmux
    子进程调用）现在跑在 `@work(thread=True)` worker 里，通过 call_from_thread 把
    结果异步写回 `pane.session_name`，不再和按键处理同步完成，测试拿到 pane 后
    不能立即读 session_name，要轮询等它就绪。"""
    import asyncio

    for _ in range(tries):
        if pane.session_name is not None:
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {tries * interval:.1f}s 后 pane.session_name 仍为 None")


def _make_store(sessions=None, extra_runtimes=()):
    sessions = sessions if sessions is not None else [
        {
            "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
            "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": f"会话{i}",
            "cwd": "/tmp", "live": False,
        }
        for i in range(3)
    ]
    claude = mock.Mock()
    claude.id = "claude"
    claude.display_name = "Claude"
    claude.is_available.return_value = True
    claude.scan_sessions.return_value = sessions
    claude.load_conversation.return_value = [
        pickup.ConversationMessage("user", "测试问题"),
        pickup.ConversationMessage("assistant", "测试回复"),
    ]
    registry = pickup.RuntimeRegistry((claude, *extra_runtimes))
    with mock.patch.object(pickup.titles, "load_cache", return_value={}):
        store = pickup.SessionStore(limit=20, registry=registry)
        store.load()
    return store, registry


class KittyKeyboardProtocolTests(unittest.TestCase):
    """回归：pickup 必须默认关闭 Textual 的 Kitty 键盘协议，否则 iTerm2/Ghostty/kitty
    等支持它的终端会把按键当转义码原样上报、绕过操作系统输入法，用户在内嵌 Agent
    里根本打不出中文（真机反馈：iTerm2 + SSH 下内嵌 Agent 无法输入中文，同一 SSH
    的 nano 却正常，唯一差别就是 pickup 开了这个协议）。pickup 顶层用
    os.environ.setdefault 在任何 textual 导入前关掉它。"""

    def test_kitty_keyboard_protocol_disabled_by_default(self) -> None:
        import os

        # import pickup 已在模块顶部发生，setdefault 应已生效
        self.assertEqual(os.environ.get("TEXTUAL_DISABLE_KITTY_KEY"), "1")
        import textual.constants as constants
        self.assertTrue(
            constants.DISABLE_KITTY_KEY,
            "Textual 必须把 Kitty 键盘协议判定为禁用；开着会绕过 IME 导致内嵌 Agent 打不了中文",
        )


class AppThemeTests(unittest.IsolatedAsyncioTestCase):
    """pickup 自身界面配色应跟随外层终端探测到的深浅色（真机反馈：浅色终端下
    配色不对——此前只处理了托管会话内的深浅色注入，没接 pickup 自己的界面）。"""

    async def test_theme_follows_detected_terminal_background(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=b"\x1b]11;rgb:ffff/ffff/ffff\x07")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-light")

    async def test_dark_background_uses_dark_theme(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-dark")

    async def test_missing_report_falls_back_to_default_dark(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "pickup-dark")

    async def test_runtime_top_bar_matches_footer_and_aligns_right(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            top_bar = app.screen.query_one("#runtime-top-bar")
            footer = app.screen.query_one(Footer)
            self.assertEqual(top_bar.styles.align_horizontal, "right")
            self.assertEqual(top_bar.styles.background, footer.styles.background)

    async def test_sidebar_and_split_panes_use_one_cell_blank_gaps(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            self.assertEqual(area.styles.margin.left, 1)
            sessions = store.all_sessions()[:2]
            area.show_hosted_group(
                "/tmp",
                [(session, None, lambda: "") for session in sessions],
            )
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            first, second = area._cells()  # noqa: SLF001
            self.assertEqual(first.styles.margin.left, 0)
            self.assertEqual(second.styles.margin.left, 1)
            self.assertEqual(first.styles.border_left[0], "")
            self.assertEqual(second.styles.border_left[0], "")
            self.assertEqual(second.styles.border_top[0], "")
            self.assertEqual(second.styles.border_right[0], "")
            self.assertEqual(second.styles.border_bottom[0], "")

    async def test_footer_does_not_bind_n_for_new_session(self) -> None:
        """底栏不再暴露 n 新建快捷键；新建只走侧边栏项 / 顶栏加格。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            keys = {b.key for b in app.screen.BINDINGS}
            self.assertNotIn("n", keys)
            actions = {b.action for b in app.screen.BINDINGS}
            self.assertNotIn("new_session", actions)

    async def test_focusing_split_pane_highlights_matching_sidebar_session(self) -> None:
        """多分屏时聚焦某一格，侧边栏高亮必须切到该格对应会话。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            list_view = app.screen.query_one(SessionListView)
            key0 = pickup.session_key(sessions[0])
            key1 = pickup.session_key(sessions[1])
            # 写入分屏记忆，避免后续列表高亮回调把两格收成单格
            app.screen._split_store.set_group("/tmp", [key0, key1], focus_key=key0)
            area.show_hosted_group(
                "/tmp",
                [
                    (session, session["keepalive_name"], lambda: "")
                    for session in sessions
                ],
                focus_key=key0,
            )
            await _wait_until(lambda: len(area._cells()) == 2)  # noqa: SLF001
            # 先保证侧边栏停在第一格会话（不动 focus，避免抢焦点）
            list_view.index = 1
            await pilot.pause()
            self.assertEqual(len(area._cells()), 2)  # noqa: SLF001
            self.assertEqual(pickup.session_key(list_view.selected_session()), key0)
            second = area._cells()[1].embed_pane()  # noqa: SLF001
            self.assertIsNotNone(second)
            second.focus()
            await pilot.pause()
            await _wait_until(
                lambda: list_view.index == 2
                and list_view.selected_session() is not None
                and pickup.session_key(list_view.selected_session()) == key1,
            )
            self.assertEqual(area.focus_key, key1)

    async def test_try_restore_startup_layout_skips_prune_before_store_loaded(self) -> None:
        """扫描未完成时不得 prune+save，否则会把磁盘分屏记忆清空。"""
        from pickup import split_layout
        import tempfile

        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        key0 = pickup.session_key(sessions[0])
        key1 = pickup.session_key(sessions[1])
        with tempfile.TemporaryDirectory() as td:
            layout_path = os.path.join(td, "split-layout.json")
            with mock.patch.object(split_layout, "LAYOUT_FILE", layout_path), \
                    mock.patch.object(split_layout, "CACHE_DIR", td):
                # 先写入一份「两格组合」到磁盘；再装 App（__init__ 会 load_layout）
                seed = split_layout.SplitLayoutStore()
                seed.set_group("/tmp", [key0, key1], focus_key=key0)
                split_layout.save_layout(seed)
                store.loaded = False  # 模拟异步首扫尚未完成
                app = PickupApp(store, embed_ok=True)
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause(delay=0.05)
                    # 显式再调一次：即便 on_mount 漏调，契约也必须守住
                    app.screen._try_restore_startup_layout()  # noqa: SLF001
                    loaded = split_layout.load_layout()
                    group = loaded.get_group(key0)
                    self.assertIsNotNone(group)
                    assert group is not None
                    self.assertEqual(group.session_keys, [key0, key1])

    async def test_closing_one_split_pane_keeps_sibling_widget(self) -> None:
        """关一格只卸该格，同伴 EmbedPane 实例不得被整排 remount 换掉。"""
        sessions = [
            {
                "source": "claude", "id": f"s{i}", "short_id": f"s{i}",
                "mtime": time.time() - i * 100, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": f"会话{i}",
                "cwd": "/tmp", "live": True,
                "keepalive_name": f"pickup-claude-s{i}",
            }
            for i in range(2)
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key0 = pickup.session_key(sessions[0])
            key1 = pickup.session_key(sessions[1])
            app.screen._split_store.set_group("/tmp", [key0, key1], focus_key=key0)  # noqa: SLF001
            area.show_hosted_group(
                "/tmp",
                [
                    (session, session["keepalive_name"], lambda: "")
                    for session in sessions
                ],
                focus_key=key0,
            )
            await _wait_until(lambda: len(area.cells()) == 2)
            keeper = area.cells()[1]
            keeper_pane = keeper.embed_pane()
            self.assertIsNotNone(keeper_pane)
            area._close_spec(area.pane_specs()[0])  # noqa: SLF001
            await _wait_until(lambda: len(area.cells()) == 1)
            self.assertIs(area.cells()[0], keeper)
            self.assertIs(area.cells()[0].embed_pane(), keeper_pane)
            self.assertEqual(area.ordered_session_keys(), [key1])

    async def test_same_hosted_identity_skips_remount_keeps_live_grid(self) -> None:
        """同 (session_key, keepalive) 再 show_hosted_group 不得整排 remount 清掉 live 画面。"""
        from pickup.embed import Cell

        sessions = [
            {
                "source": "claude", "id": "s0", "short_id": "s0",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "会话0",
                "cwd": "/tmp", "live": True,
                "keepalive_name": "pickup-claude-s0",
            }
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            area = app.screen.query_one(SplitPaneArea)
            key0 = pickup.session_key(sessions[0])
            # 阻止列表跟随在断言窗口内另行 remount
            with mock.patch.object(app.screen, "_follow_current_selection"):
                area.show_hosted_group(
                    "/tmp",
                    [
                        (sessions[0], sessions[0]["keepalive_name"], lambda: "FALLBACK-TOP"),
                    ],
                    focus_key=key0,
                )
                await _wait_until(lambda: len(area.cells()) == 1)
                pane = area.cells()[0].embed_pane()
                self.assertIsNotNone(pane)
                cell_widget = area.cells()[0]
                fake_grid = [[Cell("L")] for _ in range(3)]
                pane._grid = fake_grid  # noqa: SLF001
                pane.session_name = sessions[0]["keepalive_name"]
                pane._capture_generation = 7  # noqa: SLF001

                with mock.patch.object(
                    area, "_schedule_mount", wraps=area._schedule_mount,
                ) as mount_mock:
                    area.show_hosted_group(
                        "/tmp",
                        [
                            (
                                sessions[0],
                                sessions[0]["keepalive_name"],
                                lambda: "FALLBACK-UPDATED",
                            ),
                        ],
                        focus_key=key0,
                    )
                    mount_mock.assert_not_called()

                self.assertIs(area.cells()[0], cell_widget)
                self.assertIs(area.cells()[0].embed_pane(), pane)
                self.assertIs(pane._grid, fake_grid)  # noqa: SLF001
                self.assertEqual(pane._capture_generation, 7)  # noqa: SLF001
                self.assertEqual(pane._detail_renderer(), "FALLBACK-UPDATED")  # noqa: SLF001

    async def test_hosted_registration_keeps_session_active_without_is_alive(self) -> None:
        """store.hosted 仍登记时，is_alive 假阴性不得把会话判为不活跃。"""
        sessions = [
            {
                "source": "claude", "id": "s0", "short_id": "s0",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "会话0",
                "cwd": "/tmp", "live": False,
                "keepalive_name": "pickup-claude-s0",
            }
        ]
        store, _ = _make_store(sessions=sessions)
        key = pickup.session_key(sessions[0])
        store.hosted[key] = "pickup-claude-s0"
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            with mock.patch("pickup.embed.is_alive", return_value=False):
                self.assertTrue(app.screen._is_session_active(key))  # noqa: SLF001
                self.assertTrue(app.screen._session_is_active(sessions[0]))  # noqa: SLF001

    async def test_reconcile_split_keys_after_provisional_becomes_real(self) -> None:
        """占位卡转正后，分屏和侧边栏选择都必须迁移到真实会话。"""
        provisional_id = "abcd1234"
        real_id = "real-session-uuid"
        kname = "pickup-claude-abcd1234"
        provisional = {
            "source": "claude", "id": provisional_id, "short_id": provisional_id,
            "mtime": time.time(), "size_bytes": 0, "size_kb": 0,
            "native_title": None, "fallback_title": "新 Claude 会话",
            "cwd": "/tmp", "live": True, "keepalive_name": kname, "provisional": True,
        }
        real = {
            "source": "claude", "id": real_id, "short_id": real_id[:12],
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": "真实会话", "fallback_title": "真实会话",
            "cwd": "/tmp", "live": True, "keepalive_name": kname,
        }
        store, _ = _make_store(sessions=[provisional])
        old_key = pickup.session_key(provisional)
        new_key = pickup.session_key(real)
        app = PickupApp(store, embed_ok=True)
        # 本测手动模拟一次扫描替换；禁止后台定时重扫把 fixture 又写回占位卡。
        with mock.patch.object(store, "refresh", return_value=False):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                area = app.screen.query_one(SplitPaneArea)
                app.screen._split_store.set_group("/tmp", [old_key], focus_key=old_key)  # noqa: SLF001
                with mock.patch("pickup.embed.is_alive", return_value=True):
                    area.show_hosted_group(
                        "/tmp",
                        [(provisional, kname, lambda: "")],
                        focus_key=old_key,
                    )
                    list_view = app.screen.query_one(SessionListView)
                    await list_view.rebuild(select_key=old_key)
                    self.assertEqual(
                        pickup.session_key(list_view.selected_session()), old_key
                    )
                    store.sessions["claude"] = [real]
                    store._order = [new_key]  # noqa: SLF001 — 模拟重扫把占位卡替换成真实卡
                    store.hosted[new_key] = kname
                    await app.screen._rebuild_list()  # noqa: SLF001
                    group = app.screen._split_store.get_group(new_key)  # noqa: SLF001
                    self.assertIsNotNone(group)
                    self.assertEqual(group.session_keys, [new_key])
                    self.assertEqual(area.pane_specs()[0].session_key, new_key)
                    self.assertEqual(
                        pickup.session_key(list_view.selected_session()), new_key,
                        "占位卡转成真实卡后仍应选中同一份运行中会话",
                    )
                    self.assertEqual(area.ordered_session_keys(), [new_key])

    async def test_resize_full_repaint_is_debounced(self) -> None:
        """连续缩放手势只在停稳后触发一次整屏全量重绘，不能每次尺寸变化都狂刷。"""
        import pickup.ui.app as app_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        # CI 上 pilot.pause 的墙钟开销可能让 0.12s 防抖在「拖动断言」前就到期；
        # 本测把窗口拉长，只验证「拖动中重置、停稳后恰好一次」的契约。
        debounce = 0.5
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            calls: list[Size] = []
            original = app._force_full_repaint

            def _tracking_force() -> None:
                calls.append(app.size)
                original()

            with (
                mock.patch.object(app_mod, "_RESIZE_FULL_REPAINT_DEBOUNCE", debounce),
                mock.patch.object(app, "_force_full_repaint", side_effect=_tracking_force),
            ):
                # 快速连续缩放：防抖计时器应被反复重置，到期前不应触发全量重绘
                await pilot.resize_terminal(90, 28)
                await pilot.pause(delay=0.02)
                await pilot.resize_terminal(80, 24)
                await pilot.pause(delay=0.02)
                await pilot.resize_terminal(70, 22)
                await pilot.pause(delay=0.02)
                self.assertEqual(calls, [], "拖动过程中不应触发整屏全量重绘")
                self.assertIsNotNone(app._resize_full_repaint_timer)
                # 停稳超过防抖窗口后应恰好一次，且尺寸为最后一次目标
                await pilot.pause(delay=debounce + 0.05)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], Size(70, 22))
                self.assertIsNone(app._resize_full_repaint_timer)

    def test_compositor_index_error_recovers_instead_of_exiting(self) -> None:
        """窗口缩放时 Textual chops/spans 行数竞态：IndexError 应自愈，不退出 TUI。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        app._compositor_recovery_budget = 2
        forced: list[int] = []

        def _fake_force() -> None:
            forced.append(1)

        with mock.patch.object(app, "_force_full_repaint", side_effect=_fake_force):
            app._handle_exception(IndexError("list index out of range"))
        self.assertEqual(forced, [1])
        self.assertEqual(app._compositor_recovery_budget, 1)
        self.assertNotEqual(getattr(app, "_return_code", None), 1)

        # 额度耗尽后仍走默认致命路径，并落盘供 diagnose 读取
        app._compositor_recovery_budget = 0
        with (
            mock.patch.object(app, "_force_full_repaint", side_effect=_fake_force),
            mock.patch("textual.app.App._handle_exception") as fatal,
            mock.patch("pickup.observe.log_exception") as logged,
        ):
            app._handle_exception(IndexError("list index out of range"))
        fatal.assert_called_once()
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[0], "TUI 未捕获异常")
        self.assertEqual(forced, [1], "额度耗尽后不应再尝试整屏重绘")

    def test_fatal_tui_exception_is_logged_before_exit(self) -> None:
        """非 compositor 自愈的致命异常必须写入 observe，不能只闪在终端。"""
        from pickup import observe
        import tempfile

        store, _ = _make_store()
        with tempfile.TemporaryDirectory() as tmp:
            events_path = os.path.join(tmp, "events.log")
            embed_path = os.path.join(tmp, "embed-error.log")
            with (
                mock.patch.object(observe, "CACHE_DIR", tmp),
                mock.patch.object(observe, "EVENTS_LOG", events_path),
                mock.patch.object(observe, "EMBED_ERROR_LOG", embed_path),
            ):
                observe.reset_for_tests()
                observe.init(debug=False)
                app = PickupApp(store, embed_ok=False)

                def _boom() -> None:
                    raise NameError("name '_project_groups' is not defined")

                try:
                    _boom()
                except NameError as inner:
                    class WorkerFailed(Exception):
                        def __init__(self, error: BaseException) -> None:
                            self.error = error
                            super().__init__(f"Worker raised exception: {error!r}")

                    wrapped = WorkerFailed(inner)
                with mock.patch("textual.app.App._handle_exception"):
                    app._handle_exception(wrapped)
                last = observe.read_last_error()
                self.assertIsNotNone(last)
                assert last is not None
                self.assertEqual(last["where"], "TUI 未捕获异常")
                self.assertEqual(last["exc_type"], "NameError")
                self.assertIn("_project_groups", last["traceback"])
                self.assertIn("_boom", last["traceback"])
                self.assertIn("via WorkerFailed", last["traceback"])

    async def test_f12_saves_screenshot_under_cache(self) -> None:
        from pickup import observe
        import tempfile

        store, _ = _make_store()
        with tempfile.TemporaryDirectory() as tmp:
            events_path = os.path.join(tmp, "events.log")
            with (
                mock.patch.object(observe, "CACHE_DIR", tmp),
                mock.patch.object(observe, "EVENTS_LOG", events_path),
                mock.patch.object(observe, "EMBED_ERROR_LOG", os.path.join(tmp, "embed-error.log")),
            ):
                observe.reset_for_tests()
                observe.init(debug=False)
                app = PickupApp(store, embed_ok=False)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause(delay=0.2)
                    # 直接调 action，避免 Pilot 对 F12 键名在部分环境下不派发到 Screen。
                    app.screen.action_save_screenshot()
                    await pilot.pause(delay=0.2)
                    keys = {b.key for b in app.screen.BINDINGS}
                    self.assertIn("f12", keys)
                shots = os.path.join(tmp, "screenshots")
                files = os.listdir(shots) if os.path.isdir(shots) else []
                self.assertTrue(any(name.endswith(".svg") for name in files), files)
                self.assertTrue(os.path.isfile(events_path))
                with open(events_path, encoding="utf-8") as fh:
                    body = fh.read()
                self.assertIn('"name": "screenshot"', body)

    async def test_embed_pane_background_matches_real_terminal_bg(self) -> None:
        """回归测试：内嵌 Agent 画面里的"默认背景"格子（tmux 报 bg=-1）必须垫在
        外层终端真实底色上，不能透出 Textual 主题的中性灰——否则整个托管画面看
        着变灰（真机反馈：内嵌 agent tui 背景变中性灰）。断言面板底色 == OSC 11
        探到的真实 RGB。"""
        from pickup.ui.embed_pane import EmbedPane

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            pane = _primary_embed_pane(app.screen)
            self.assertEqual(pane.styles.background.rgb, (0x1e, 0x1e, 0x2e))


class MainScreenWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """主屏退出时不能被长驻刷新线程或首屏等待线程拖住。"""

    async def test_background_refresh_worker_is_cancelled_on_normal_exit(self) -> None:
        store, _ = _make_store()
        store.refresh = mock.Mock(return_value=False)
        app = PickupApp(store, embed_ok=False)

        started_at = time.monotonic()
        with (
            mock.patch("pickup.ui.main_screen.REFRESH_INTERVAL", 0.01),
            mock.patch("pickup.ui.main_screen.REFRESH_INTERVAL_MAX", 0.02),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                await _wait_until(lambda: store.refresh.call_count > 0)
                worker = next(w for w in app.workers if w.group == "session-refresh")
                await pilot.press("escape")
                await pilot.pause()

        self.assertTrue(worker.is_cancelled)
        self.assertLess(time.monotonic() - started_at, 8.0)

    async def test_initial_load_wait_worker_is_cancelled_on_normal_exit(self) -> None:
        runtime = mock.Mock(id="claude", display_name="Claude")
        registry = pickup.RuntimeRegistry((runtime,))
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)
        app = PickupApp(store, embed_ok=False)

        started_at = time.monotonic()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.05)
            worker = next(w for w in app.workers if w.group == "initial-load")
            await pilot.press("escape")
            await pilot.pause()

        self.assertTrue(worker.is_cancelled)
        self.assertLess(time.monotonic() - started_at, 8.0)


class SessionStoreFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_failure_reaches_terminal_state_and_refresh_recovers(self) -> None:
        runtime = mock.Mock(id="claude", display_name="Claude")
        registry = mock.MagicMock()
        registry.ids = ("claude",)
        registry.get.return_value = runtime
        registry.__iter__.side_effect = lambda: iter((runtime,))
        registry.scan_all.side_effect = [RuntimeError("历史目录暂时不可读"), {"claude": []}]
        with mock.patch.object(pickup.titles, "load_cache", return_value={}):
            store = pickup.SessionStore(limit=20, registry=registry)

        store.load()

        self.assertTrue(store.loaded)
        self.assertTrue(store.wait_loaded(timeout=0))
        self.assertIn("Failed to load sessions", store.get_load_error())
        self.assertIn("历史目录暂时不可读", store.get_load_error())

        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.1)
            search = app.screen.query_one("#project-search", Input)
            self.assertIn("Failed to load sessions", search.placeholder)
            self.assertIn("retrying", search.placeholder)

            self.assertFalse(store.refresh())
            self.assertIsNone(store.get_load_error())
            app.screen._update_header()
            self.assertNotIn("Failed", search.placeholder)
            self.assertNotIn("retrying", search.placeholder)


class SessionStoreRemoveSessionTests(unittest.TestCase):
    """store.remove_session：删除动作成功后立即摘除内存状态，不必等下一轮 refresh()。"""

    def test_remove_session_clears_every_key_indexed_structure(self) -> None:
        store, _ = _make_store()
        key = "claude:s0"
        session = store.find_session(key)
        self.assertIsNotNone(session)
        # 人为塞满所有按 key 索引的结构，验证 remove_session 逐一清干净。
        store.display_titles[key] = "标题"
        store.generating.add(key)
        store.conversations[key] = (1.0, [])
        store.hosted[key] = "pickup-claude-fake"
        store._provisional[key] = dict(session)
        store._force_ended.add(key)

        store.remove_session(key)

        self.assertIsNone(store.find_session(key))
        self.assertNotIn(key, store._order)
        self.assertNotIn(key, store.display_titles)
        self.assertNotIn(key, store.generating)
        self.assertNotIn(key, store.conversations)
        self.assertNotIn(key, store.hosted)
        self.assertNotIn(key, store._provisional)
        self.assertNotIn(key, store._force_ended)

    def test_remove_session_leaves_other_sessions_untouched(self) -> None:
        store, _ = _make_store()
        store.remove_session("claude:s0")
        self.assertIsNotNone(store.find_session("claude:s1"))
        self.assertIsNotNone(store.find_session("claude:s2"))


class SessionCardVisualTests(unittest.TestCase):
    """侧边栏两行卡片的列布局和状态样式不能随刷新优化再次回退。"""

    @staticmethod
    def _card(
        *,
        live=False,
        keepalive_name=None,
        generating=False,
        source="opencode",
        display_name="OpenCode",
        display_title="修复侧边栏展示",
        cwd="/tmp/pickup",
    ) -> SessionCard:
        runtime = mock.Mock(id=source, display_name=display_name)
        store = mock.Mock()
        store.registry.get.return_value = runtime
        session = {
            "source": source,
            "id": "visual-check",
            "fallback_title": display_title,
            "cwd": cwd,
            "mtime": time.time(),
            "live": live,
        }
        if keepalive_name is not None:
            session["keepalive_name"] = keepalive_name
        return SessionCard(
            session,
            store,
            "◐",
            display_title=display_title,
            is_generating=generating,
        )

    def test_runtime_is_right_aligned_on_second_line_at_fixed_width(self) -> None:
        card = self._card()
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("OpenCode", lines[0])
        self.assertTrue(lines[1].endswith("OpenCode"))
        self.assertEqual(pickup._text_width(lines[0]), 39)
        self.assertEqual(pickup._text_width(lines[1]), 39)
        relative_time = pickup._format_relative_time(card.session["mtime"])
        self.assertTrue(lines[2].endswith(relative_time))

    def test_long_title_uses_ellipsis_without_sharing_runtime_line(self) -> None:
        card = self._card(
            display_title="这是一个非常非常非常长的侧边栏标题用来验证省略",
        )
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        self.assertNotIn("OpenCode", lines[0])
        self.assertTrue(lines[1].endswith("OpenCode"))
        self.assertIn("...", lines[0])
        self.assertEqual(pickup._text_width(lines[0]), 39)

    def test_runtime_label_uses_distinct_brand_colors(self) -> None:
        cases = (
            ("claude", "Claude", "#D97757"),
            ("codex", "Codex", "#60A5FA"),
            ("cursor", "Cursor", "#A78BFA"),
            ("kimi", "Kimi", "#F472B6"),
            ("opencode", "OpenCode", "#34D399"),
        )
        for source, display_name, color in cases:
            card = self._card(source=source, display_name=display_name)
            with self.subTest(source=source), mock.patch.object(
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
            ):
                rendered = card.render()
            runtime_start = rendered.plain.index(display_name)
            runtime_spans = [
                span for span in rendered.spans
                if span.start <= runtime_start < span.end
            ]
            self.assertTrue(
                any(color.lower() in str(span.style).lower() for span in runtime_spans),
                f"{source} runtime should use {color}, spans={runtime_spans}",
            )
            self.assertTrue(
                any("bold" in str(span.style).lower() for span in runtime_spans),
                f"{source} runtime label should be bold, spans={runtime_spans}",
            )

    def test_project_name_is_bold_but_title_is_not(self) -> None:
        """侧边栏「项目名: 标题」：项目名 bold、标题 dim，形成可见对比。"""
        card = self._card(cwd="/tmp/pickup", display_title="修复侧边栏展示")
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        first_line = lines[0]
        second_line = lines[1]
        project_start = first_line.index("pickup")
        project_end = project_start + len("pickup")
        title_start = first_line.index("修复侧边栏展示")
        runtime_start = second_line.rfind("OpenCode")

        project_spans = [
            span for span in rendered.spans
            if span.start <= project_start and span.end >= project_end
        ]
        self.assertTrue(
            any("bold" in str(span.style).lower() for span in project_spans),
            f"project name should be bold, spans={project_spans}",
        )
        title_spans = [
            span for span in rendered.spans
            if span.start <= title_start < span.end and span.end <= len(first_line)
        ]
        self.assertTrue(
            any("dim" in str(span.style).lower() for span in title_spans),
            f"title should be dim, spans={title_spans}",
        )
        self.assertFalse(
            any("bold" in str(span.style).lower() for span in title_spans),
            f"title should not be bold, spans={title_spans}",
        )

    def test_generating_title_keeps_spinner_without_bold(self) -> None:
        card = self._card(generating=True)
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
        ):
            rendered = card.render()

        lines = rendered.plain.splitlines()
        first_line = lines[0]
        runtime_start = lines[1].rfind("OpenCode")
        self.assertTrue(first_line.startswith("◐ "))
        # spinner 本身不加粗；项目名允许 bold（与标题对比）。
        spinner_end = len("◐ ")
        spinner_spans = [
            span for span in rendered.spans
            if span.start < spinner_end and span.end <= len(first_line)
        ]
        self.assertFalse(
            any(
                "bold" in str(span.style).lower() and span.end <= spinner_end
                for span in spinner_spans
            )
        )

    def test_running_status_is_green_but_ended_status_is_not(self) -> None:
        cases = (
            (self._card(live=True), "Running", True),
            (self._card(keepalive_name="pickup-opencode-visual"), "Running (hosted)", True),
            (self._card(), "Ended", False),
        )
        for card, status_text, expected_green in cases:
            with self.subTest(status=status_text), mock.patch.object(
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 3),
            ):
                rendered = card.render()

            lines = rendered.plain.splitlines()
            status_start = rendered.plain.index("\n") + 1
            status_end = status_start + len(status_text)
            # 运行中用略降饱和的成功绿，不用 ANSI green
            green_spans = [
                span for span in rendered.spans
                if "#3f9a6a" in str(span.style).lower()
                and span.start <= status_start
                and span.end >= status_end
            ]
            self.assertEqual(bool(green_spans), expected_green)


class SidebarVisualLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_card_spacing_are_explicit(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            search = app.screen.query_one("#project-search", Input)
            list_view = app.screen.query_one(SessionListView)
            items = list(list_view.children)
            cards = list(app.screen.query(SessionCard))

            # 筛选条用表面层弱化文字，不再白字铺高饱和主色底
            self.assertEqual(search.styles.background, Color.parse("#1C2430"))
            self.assertLess(search.styles.color.a, 1.0)
            self.assertEqual(search.region.height, 2)  # 正文 1 + 末行间隔 1
            self.assertGreaterEqual(len(items), 3)
            # 分隔空行画在卡片自身内，两项 region 紧挨、无外边距空隙
            self.assertEqual(items[1].region.y - items[0].region.bottom, 0)
            self.assertEqual(items[2].region.y - items[1].region.bottom, 0)
            self.assertTrue(cards)
            self.assertTrue(all(card.region.height == 3 for card in cards))
            from pickup.ui.session_list import NewSessionCard
            new_card = app.screen.query_one(NewSessionCard)
            self.assertEqual(new_card.region.height, 2)
            # 搜索框底边紧贴新建项顶边（搜索的末行间隔已含在 height: 2 内）
            self.assertEqual(items[0].region.y - search.region.bottom, 0)


class MainScreenNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_selection_and_project_search_filter(self) -> None:
        """侧边栏顶部搜索框：大小写无关模糊匹配项目名，并同步过滤会话列表。"""
        sessions = [
            {
                "source": "claude", "id": "a", "short_id": "a",
                "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "节点选择",
                "cwd": "/Users/x/ProxyAgent", "live": False,
            },
            {
                "source": "claude", "id": "b", "short_id": "b",
                "mtime": time.time() - 10, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "界面打磨",
                "cwd": "/Users/x/pickup", "live": False,
            },
            {
                "source": "claude", "id": "c", "short_id": "c",
                "mtime": time.time() - 20, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "字幕优化",
                "cwd": "/Users/x/LiveCaptionMac", "live": False,
            },
        ]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            search = app.screen.query_one("#project-search", Input)
            self.assertEqual(list_view.index, 0)  # 固定「+新建会话」项
            self.assertEqual(len(list_view.visible_sessions()), 3)
            self.assertIn("Filter projects", search.placeholder)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(list_view.index, 1)

            await pilot.press("slash")
            await pilot.pause()
            self.assertTrue(search.has_focus)
            await pilot.press("p", "r", "o", "x", "y")
            await pilot.pause()
            self.assertEqual(list_view.nav.project_query, "proxy")
            visible = list_view.visible_sessions()
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], "a")
            self.assertIn("ProxyAgent", visible[0]["cwd"])

            # 清空后恢复全部；Esc 在搜索框有内容时只清查询，不退出
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(list_view.nav.project_query, "")
            self.assertEqual(len(list_view.visible_sessions()), 3)
            self.assertIsNone(app.return_value)

            # 会话标题也可被模糊命中
            search.focus()
            await pilot.pause()
            search.value = "界面"
            await pilot.pause()
            visible = list_view.visible_sessions()
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["id"], "b")

    async def test_enter_without_embed_exits_with_launch_request(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)
        self.assertEqual(app.return_value.session["id"], "s0")

    async def test_escape_exits_with_no_result(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("escape")
            await pilot.pause()
        self.assertIsNone(app.return_value)

    async def test_clicking_session_card_selects_and_launches_without_crashing(self) -> None:
        """回归测试：真机实测过点击会话卡片直接闪退——Textual 默认给所有 Widget
        开启内置的鼠标拖拽文本选择（ALLOW_SELECT=True），会话卡片这类自定义
        展示型 Widget 被点击时触发该逻辑，在某些时序下 container 解析为 None，
        访问 .region 抛 AttributeError 崩溃整个应用。修法是全局关闭
        ALLOW_SELECT（PickupApp/SessionCard/NewSessionCard/EmbedPane 均已设置），
        这里钉死「点击等价于 Enter」的行为不能再回归成崩溃。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            card = app.screen.query(SessionCard).first()
            clicked = await pilot.click(card, offset=(5, 0))
            await pilot.pause()
            self.assertTrue(clicked)
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)

    async def test_list_item_gap_padding_is_part_of_hit_area(self) -> None:
        """会话卡第三行（时间行）仍属本卡命中区；不要用 ListItem margin/padding 做分隔。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        self.assertNotIn("margin-bottom:", PickupApp.CSS)
        self.assertNotIn("padding-bottom:", PickupApp.CSS)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            card = app.screen.query(SessionCard).first()
            self.assertEqual(card.region.height, 3)
            # 点第三行时间行，应等价于点该会话卡
            clicked = await pilot.click(card, offset=(5, 2))
            await pilot.pause()
            self.assertTrue(clicked)
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)

    async def test_rebuild_updates_in_place_when_session_set_unchanged(self) -> None:
        """性能优化回归：会话集合（顺序+成员）没变、只是某个会话内容变了（比如
        「运行中」翻转成「已结束」）时，`rebuild()` 必须走原地更新——不清空/
        重建 ListView 子项，只换 SessionCard 手上的 session 引用再按需
        `refresh()`。这里同时断言两件事：① 卡片 Widget 实例本身没有被销毁重建
        （identity 不变）；② 渲染出的实际文本确实反映了新状态——只断言内部
        状态不能证明渲染结果对，这是 docs/MAINTAINER_GUIDE.md 记录过的教训。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards_before = list_view._session_cards()
            self.assertEqual(len(cards_before), 3)
            self.assertNotIn("Running", cards_before[0].render().plain)

            # 模拟一次后台重扫：s0 的会话字典被替换成新对象（和真实 _merge_scanned
            # 行为一致，扫描结果每次都是新 dict），但会话键集合/顺序没变，
            # 只是 live 从 False 翻到 True。
            old_session = store.sessions["claude"][0]
            new_session = dict(old_session, live=True)
            store.sessions["claude"][0] = new_session

            await list_view.rebuild()

            cards_after = list_view._session_cards()
            self.assertEqual(
                [id(c) for c in cards_before], [id(c) for c in cards_after],
                "会话集合没变时不应该重新 mount 任何 SessionCard 实例",
            )
            self.assertIs(cards_after[0].session, new_session)
            self.assertIn("Running", cards_after[0].render().plain)

    async def test_refresh_detects_detail_changes_and_updates_card_and_pane_in_place(self) -> None:
        old_session = {
            "source": "claude", "id": "detail", "short_id": "detail",
            "mtime": 100.0, "size_bytes": 1, "size_kb": 1,
            "native_title": "旧标题", "fallback_title": "旧标题",
            "cwd": "/tmp/pickup", "live": False, "path": "/tmp/pickup-detail.jsonl",
            "first_user_msg": "旧首问", "last_user_msg": "旧问题",
            "last_agent_msg": "旧回复",
        }
        new_session = dict(
            old_session,
            mtime=200.0,
            native_title="新标题",
            fallback_title="新标题",
            last_user_msg="新问题",
            last_agent_msg="新回复",
        )
        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.side_effect = [[old_session], [new_session]]
        runtime.load_conversation.side_effect = [
            [
                pickup.ConversationMessage("user", "旧问题"),
                pickup.ConversationMessage("assistant", "旧回复"),
            ],
            [
                pickup.ConversationMessage("user", "新问题"),
                pickup.ConversationMessage("assistant", "新回复"),
            ],
        ]
        registry = pickup.RuntimeRegistry((runtime,))
        with (
            mock.patch.object(pickup.titles, "load_cache", return_value={}),
            mock.patch.object(pickup.keepalive, "annotate"),
        ):
            store = pickup.SessionStore(limit=20, registry=registry)
            store.load()

        original_signature = store._sessions_signature()
        store.sessions["claude"][0]["mtime"] = 101.0
        self.assertNotEqual(store._sessions_signature(), original_signature)
        store.sessions["claude"][0]["mtime"] = 100.0
        store.sessions["claude"][0]["last_user_msg"] = "另一条问题"
        self.assertNotEqual(store._sessions_signature(), original_signature)
        store.sessions["claude"][0]["last_user_msg"] = "旧问题"

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            screen = app.screen
            list_view = screen.query_one(SessionListView)
            pane = _primary_embed_pane(screen)
            card_before = list_view._session_cards()[0]
            await _wait_until(lambda: "旧标题" in pane.render().plain)
            old_snapshot = store.sessions["claude"][0]

            with mock.patch.object(pickup.keepalive, "annotate"):
                self.assertTrue(store.refresh())
            # 历史 path 未变时对话缓存仍有效；清掉让右栏重新 warm 到新对话。
            store.conversations.clear()
            await screen._rebuild_list()
            await pilot.pause(delay=0.3)

            card_after = list_view._session_cards()[0]
            self.assertIs(card_after, card_before)
            self.assertIsNot(card_after.session, old_snapshot)
            self.assertEqual(card_after.session["mtime"], 200.0)
            await _wait_until(lambda: "新标题" in pane.render().plain and "新问题" in pane.render().plain)
            detail = pane.render().plain
            self.assertIn("新标题", detail)
            self.assertIn("新问题", detail)
            self.assertIn("新回复", detail)
            self.assertNotIn("旧问题", detail)
            self.assertNotIn("最近提问", detail)

    async def test_rebuild_falls_back_to_full_rebuild_when_session_set_changes(self) -> None:
        """回归测试：新增/删除会话导致集合真的变了时，`rebuild()` 必须仍然正确
        走批量清空重建路径，不能被上面的原地更新优化误伤。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertEqual(len(list_view._session_cards()), 3)

            new_session = {
                "source": "claude", "id": "s99", "short_id": "s99",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "全新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()

            cards = list_view._session_cards()
            self.assertEqual(len(cards), 4)
            self.assertIn("claude:s99", [pickup.session_key(c.session) for c in cards])

    async def test_rebuild_keeps_focus_on_same_session_when_new_session_appears(self) -> None:
        """回归测试：真实反馈——聚焦第三条会话时后台刷出一条新会话，高亮和
        右栏会跟着「串位」跳到相邻的第二条。根因是 `rebuild()` 曾用
        `selected_session()`（按刚重算过的 `visible_sessions()` 索引 DOM 下标）
        推导原选中键；新会话按 mtime 置顶插入后同一下标已指向别的会话。
        `_displayed_selected_key()` 改按已渲染的 DOM 卡片取键，必须确保新会话
        置顶插入后，原选中会话仍被选中（只是位置下移），不能串到相邻会话。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.index = 2  # 选中 s1（第二条，位置 1）
            await pilot.pause()
            target_key = pickup.session_key(list_view.selected_session())
            self.assertEqual(target_key, "claude:s1")

            new_session = {
                "source": "claude", "id": "s_new", "short_id": "s_new",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()

            self.assertEqual(
                pickup.session_key(list_view.selected_session()), target_key,
                "新会话置顶插入后，原选中会话应仍被选中（只是位置下移），"
                "不能串到相邻会话",
            )

    async def test_rebuild_keeps_embed_pane_following_same_session_when_new_session_appears(
        self,
    ) -> None:
        """同一 bug 的右栏视角：右栏详情预览必须跟着原选中会话一起下移，
        不能因为高亮串位而展示成相邻会话的内容。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            list_view.index = 2  # 选中 s1
            await pilot.pause(delay=0.2)
            await _wait_until(lambda: "会话1" in _primary_embed_pane(app.screen).render().plain)

            new_session = {
                "source": "claude", "id": "s_new", "short_id": "s_new",
                "mtime": time.time() + 1000, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "新会话",
                "cwd": "/tmp", "live": False,
            }
            store.sessions["claude"].append(new_session)

            await list_view.rebuild()
            await app.screen._rebuild_list()
            await pilot.pause(delay=0.2)

            await _wait_until(lambda: "会话1" in _primary_embed_pane(app.screen).render().plain)
            self.assertNotIn("会话0", _primary_embed_pane(app.screen).render().plain)

    async def test_tick_spinner_skips_snapshot_when_nothing_generating(self) -> None:
        """`_tick_spinner` 每 150ms 触发一次；没有会话在生成标题时必须直接
        跳过，连 `store.snapshot()`（拿锁+拷贝 dict/set）都不该调用。"""
        store, _ = _make_store()
        store.generating.clear()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            with mock.patch.object(store, "snapshot", wraps=store.snapshot) as spy:
                list_view._tick_spinner()
                spy.assert_not_called()

    async def test_tick_spinner_refreshes_only_generating_cards(self) -> None:
        """有会话在生成标题时，只应该刷新命中 `generating` 的那几张卡片，
        其余卡片不应该被触碰（不遍历全部子项逐个 refresh）。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            cards = list_view._session_cards()
            store.generating.clear()
            store.generating.add("claude:s0")
            for card in cards:
                card.refresh = mock.Mock(wraps=card.refresh)

            list_view._tick_spinner()

            hit = next(c for c in cards if pickup.session_key(c.session) == "claude:s0")
            others = [c for c in cards if c is not hit]
            self.assertTrue(hit.refresh.called)
            for card in others:
                card.refresh.assert_not_called()

    async def test_enter_keeps_list_focus_until_pane_clicked(self) -> None:
        """回车/点选只挂右栏画面，不把键盘焦点抢走；点右栏后才进入内嵌交互。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)
        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-s0"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                list_view = app.screen.query_one(SessionListView)

                await pilot.press("down")
                await pilot.press("enter")
                await _wait_until(lambda: app.screen._host_pending == 0)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                self.assertTrue(list_view.has_focus)
                self.assertFalse(pane.has_focus)

                # 托管成功后列表重建仍可能排在下一帧；等 DOM 稳定并重新取当前 pane，
                # 避免点击刚被替换掉的旧 Widget。
                await pilot.pause(delay=0.2)
                pane = await _wait_for_embed_session(app.screen, "pickup-claude-s0")
                await pilot.click(pane)
                await pilot.pause()
                self.assertTrue(pane.has_focus)
                self.assertFalse(list_view.has_focus)

    async def test_right_pane_wheel_scrolls_while_list_focused(self) -> None:
        """焦点在侧边栏时，鼠标在右栏滚轮仍应滚动静态预览（与焦点无关）。"""
        store, registry = _make_store()
        long_body = "\n".join(f"行{i} " + ("内容" * 20) for i in range(80))
        registry.get("claude").load_conversation.return_value = [
            pickup.ConversationMessage("user", long_body),
            pickup.ConversationMessage("assistant", long_body),
        ]
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            list_view = app.screen.query_one(SessionListView)
            pane = _primary_embed_pane(app.screen)
            # 预览默认钉在最新（底部）；等末行可见后再上滚看更早内容
            await _wait_until(
                lambda: pane._is_detail_view() and pane.detail_offset == pane._detail_max_offset() > 0,
            )
            self.assertTrue(list_view.has_focus)
            self.assertFalse(pane.has_focus)
            before = pane.detail_offset
            # 滚轮处理不检查 has_focus；列表聚焦时直接投递也应能滚右栏预览。
            pane._on_mouse_scroll_up(
                events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False),
            )
            await pilot.pause()
            self.assertLess(pane.detail_offset, before)
            self.assertTrue(list_view.has_focus)


class MainScreenHostWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_is_single_flight_and_success_updates_current_store_session(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        started = threading.Event()
        release = threading.Event()

        def delayed_host(*args, **kwargs):
            started.set()
            # 全量套件下 Textual 调度可能挤占超过 1s；超时后若仍返回成功名，
            # 会在测试替换 store 会话对象之前就 mark_hosted 旧 dict，随后
            # dict(old, …) 把 keepalive_name 拷进新对象，断言两边都有名字。
            if not release.wait(timeout=5.0):
                raise TimeoutError("测试未能及时释放 delayed_host")
            return "pickup-claude-s0"

        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.host_session", side_effect=delayed_host) as host_mock:
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                old_request_session = store.sessions["claude"][0]

                await pilot.press("down")
                await pilot.press("enter")
                await _wait_until(started.is_set)
                self.assertTrue(app.screen._host_pending > 0)

                # 第一次托管还没结束时重复确认，只响铃，不应再启动第二个进程。
                await pilot.press("enter")
                await pilot.pause(delay=0.05)
                self.assertEqual(host_mock.call_count, 1)

                current_session = dict(old_request_session, mtime=old_request_session["mtime"] + 1)
                store.sessions["claude"][0] = current_session
                release.set()
                await _wait_until(lambda: app.screen._host_pending == 0)

                self.assertEqual(host_mock.call_count, 1)
                self.assertEqual(current_session.get("keepalive_name"), "pickup-claude-s0")
                self.assertNotIn("keepalive_name", old_request_session)
                pane = await _wait_for_embed_pane(app.screen)
                await _wait_until(lambda: pane.session_name == "pickup-claude-s0")

    async def test_host_failure_releases_single_flight_guard(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("pickup.embed.host_session", side_effect=RuntimeError("模拟启动失败")) as host_mock,
            mock.patch.object(pickup, "_log_embed_error"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                self.assertEqual(app.screen._host_pending, 0)

    async def test_new_session_request_hosts_without_reading_session(self) -> None:
        """回归：NewSessionRequest 托管成功回调不得访问 request.session。

        底栏 n 快捷键已删除；空白新建仍经 `_embed_open(NewSessionRequest)`
        （侧边栏新建项 / 顶栏加格），这条回调契约必须继续成立。
        """
        store, registry = _make_store()
        registry.build_new_session_plan = lambda request: LaunchPlan(("claude",), "/tmp")
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("pickup.embed.host_session", return_value="pickup-claude-new"),
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                app.screen._embed_open(
                    pickup.NewSessionRequest("claude", "/tmp"),
                    add_pane=False,
                )
                await _wait_until(lambda: app.screen._host_pending == 0)
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-claude-new"
                        for s in app.screen.query_one(SessionListView).visible_sessions()
                    ),
                    tries=500,
                )
                await _wait_for_embed_session(app.screen, "pickup-claude-new")

    async def test_cross_runtime_handoff_shows_hosted_card_and_keeps_embed(self) -> None:
        """回归：Claude→Cursor 接力后左栏立刻出现托管卡，右栏保持新 embed。

        真机实报：按 a 选 Cursor 后 host_session 成功，但 Cursor 卡在 Workspace Trust、
        尚未落盘 chat 时扫描器无条目；跨运行时路径又不 mark_hosted，左栏不冒新卡。
        同时 `_rebuild_list` → `_follow_current_selection` 因仍选中源 Claude，把右栏
        盖回对话预览，看起来像「什么都没发生」。
        """
        cursor = mock.Mock()
        cursor.id = "cursor"
        cursor.display_name = "Cursor"
        cursor.is_available.return_value = True
        cursor.scan_sessions.return_value = []
        cursor.load_conversation.return_value = []
        store, registry = _make_store(extra_runtimes=(cursor,))
        registry.build_launch_plan = lambda request: LaunchPlan(("agent", "--force", "prompt"), "/tmp")
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch(
                "pickup.embed.host_session", return_value="pickup-cursor-handoff"
            ) as host_mock,
            mock.patch("pickup.embed.is_alive", return_value=True),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                pane = _primary_embed_pane(app.screen)
                list_view = app.screen.query_one(SessionListView)

                await pilot.press("down")
                await pilot.press("a")
                await pilot.pause()
                self.assertIsInstance(app.screen, RuntimePickerModal)
                await pilot.press("down")  # claude 原生恢复 → cursor
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: app.screen._host_pending == 0)
                # 等 call_next(_rebuild_list) 跑完
                await _wait_until(
                    lambda: any(
                        s.get("keepalive_name") == "pickup-cursor-handoff"
                        for s in list_view.visible_sessions()
                    )
                )
                await pilot.pause(delay=0.05)

                hosted = [
                    s for s in list_view.visible_sessions()
                    if s.get("keepalive_name") == "pickup-cursor-handoff"
                ]
                self.assertEqual(len(hosted), 1, "左栏应立刻出现 Cursor 托管占位卡")
                self.assertEqual(hosted[0].get("source"), "cursor")
                selected = list_view.selected_session()
                self.assertIsNotNone(selected)
                self.assertEqual(selected.get("keepalive_name"), "pickup-cursor-handoff")
                pane = await _wait_for_embed_pane(app.screen)
                await _wait_until(lambda: pane.session_name == "pickup-cursor-handoff")
                self.assertTrue(list_view.has_focus)
                self.assertFalse(pane.has_focus)


class PaneCellHeaderSyncTests(unittest.TestCase):
    """分栏标题栏在重建中间态可能缺失；焦点同步不得因此崩掉（真机：双击顶栏 OpenCode）。"""

    def test_pane_header_title_update_before_compose(self) -> None:
        from pickup.ui.split_pane_area import _PaneHeader

        header = _PaneHeader("旧标题", lambda: None)
        header.set_title("新标题")

        title_widget = list(header.compose())[0]
        self.assertEqual(str(title_widget.render()), "新标题")

    def test_sync_active_marker_tolerates_missing_header(self) -> None:
        from pickup.ui.split_pane_area import PaneCell, PaneSpec

        cell = PaneCell(
            PaneSpec(session_key="k", cell_id="c1"),
            title="t",
            on_close=lambda: None,
            on_focus_list=lambda: None,
            osc_report=None,
        )
        # 未 mount / 未 compose：子节点为空
        cell._sync_active_marker()  # noqa: SLF001 — 不得抛 NoMatches
        cell.set_title("new-title")
        self.assertEqual(cell._title, "new-title")  # noqa: SLF001


@unittest.skipUnless(HAS_TMUX, "内嵌面板依赖真实 tmux")
class MainScreenEmbedFlowTests(unittest.IsolatedAsyncioTestCase):
    """用真实（但轻量）tmux 会话验证 MainScreen ↔ EmbedPane ↔ embed.py 接线。"""

    def setUp(self) -> None:
        self._hosted_names: list[str] = []
        self.addCleanup(self._cleanup_hosted)

    def _cleanup_hosted(self) -> None:
        for name in self._hosted_names:
            subprocess.run(["tmux", "-L", "pickup-keepalive", "kill-session", "-t", name],
                            stderr=subprocess.DEVNULL)

    async def test_first_frame_never_exposes_connecting_state(self) -> None:
        """抓帧尚未完成时也要即时展示已有详情或空白终端，不能出现连接中间态。"""
        store, _registry = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None):
            async with app.run_test(size=(120, 30)):
                pane = _primary_embed_pane(app.screen)

                pane.focus_session("已有会话", lambda: "即时会话详情")
                self.assertEqual(pane.render().plain, "即时会话详情")

                pane.focus_session("刚启动的新会话")
                self.assertEqual(pane.render().plain, "")
                self.assertNotIn("连接中", pane.render().plain)

    async def test_hosted_fallback_pins_long_conversation_to_bottom(self) -> None:
        """托管首帧前的长对话回退必须钉底，可见区不得出现最早消息。"""
        from rich.text import Text as RichText

        early = "EARLY-MSG-UNIQUE"
        late = "LATE-MSG-UNIQUE"
        lines = [early] + [f"mid-{i}" for i in range(80)] + [late]
        body = RichText("\n".join(lines))
        store, _registry = _make_store()
        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False), \
             mock.patch("pickup.embed.capture", return_value=None):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                pane = _primary_embed_pane(app.screen)
                await _wait_until(lambda: pane.size.height >= 10 and pane.size.width >= 40)
                # 挡住列表跟随，避免 focus_session 后被盖回静态预览
                with mock.patch.object(app.screen, "_follow_current_selection"):
                    pane.focus_session("pickup-cursor-x", lambda: body)
                    self.assertTrue(pane._detail_stick_bottom)
                    self.assertTrue(pane._is_hosted_fallback())
                    pane._pin_detail_to_bottom()
                    strips = pane._ensure_static_strips()
                    visible = "\n".join(s.text for s in strips)
                    self.assertIn(late, visible)
                    self.assertNotIn(early, visible)

    async def test_enter_hosts_session_and_pane_shows_live_output(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-UI-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            pane = await _wait_for_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            self.assertNotIn("连接中", pane.render().plain)
            await _wait_for_pane_text(pane, "HELLO-UI-TEST")
            list_view = app.screen.query_one(SessionListView)
            self.assertTrue(list_view.has_focus)
            self.assertFalse(pane.has_focus)
            cell = app.screen.query_one(SplitPaneArea)._cells()[0]  # noqa: SLF001
            title = cell.query_one(".title")
            header = cell.query_one(".header")
            self.assertFalse(header.has_class("-active"))
            self.assertFalse(title.render().plain.startswith("● "))

            # 点右栏后才聚焦内嵌会话；ctrl+backslash 回列表，'c' 关闭分栏
            await pilot.click(pane)
            await pilot.pause()
            self.assertTrue(pane.has_focus)
            self.assertTrue(header.has_class("-active"))
            self.assertFalse(title.render().plain.startswith("● "))
            await pilot.press("ctrl+backslash")
            await pilot.pause()
            self.assertFalse(header.has_class("-active"))
            await pilot.press("c")
            await pilot.pause()
            area = app.screen.query_one(SplitPaneArea)
            self.assertEqual(area.pane_count(), 0)

        from pickup import embed
        self.assertTrue(embed.is_alive(self._hosted_names[0]))

    async def test_reselecting_static_session_keeps_live_frame(self) -> None:
        """重复高亮同一个静止会话不能清空画面后永久停在“连接中…”。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STATIC-RESELECT-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STATIC-RESELECT-TEST")

            generation = pane._capture_generation
            pane.focus_session(pane.session_name)
            await pilot.pause(delay=0.4)

            self.assertEqual(pane._capture_generation, generation)
            self.assertIn("STATIC-RESELECT-TEST", pane.render().plain)
            self.assertNotIn("连接中", pane.render().plain)

    async def test_fast_detail_round_trip_forces_static_frame_reparse(self) -> None:
        """抓帧线程来不及观察中间态时，版本变化也必须让同名静止帧重新解析。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STATIC-ROUND-TRIP-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STATIC-ROUND-TRIP-TEST")

            name = pane.session_name
            pane.show_detail(lambda: "临时详情")
            pane.focus_session(name)
            await _wait_for_pane_text(pane, "STATIC-ROUND-TRIP-TEST")

            self.assertIn("STATIC-ROUND-TRIP-TEST", pane.render().plain)

    async def test_stale_capture_callback_cannot_overwrite_new_view(self) -> None:
        """旧会话已排队的抓帧回调不能覆盖随后打开的详情页。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'STALE-CALLBACK-TEST\\n'; cat"), None
        )

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            pane = _primary_embed_pane(app.screen)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "STALE-CALLBACK-TEST")

            old_generation = pane._capture_generation
            old_name = pane.session_name
            old_grid = pane._grid
            pane.show_detail(lambda: "新的详情页")
            pane._apply_capture(old_generation, old_name, old_grid, None, None)

            self.assertEqual(pane.render().plain, "新的详情页")

    async def test_capture_thread_recovers_after_unexpected_parse_error(self) -> None:
        """单帧解析异常只能丢一帧，抓帧线程必须继续并自动重试。"""
        from pickup import embed

        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'CAPTURE-RECOVERY-TEST\\n'; cat"), None
        )
        original_parse_screen = embed.parse_screen
        parse_calls = 0

        def flaky_parse_screen(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            if parse_calls == 1:
                raise RuntimeError("模拟单帧解析失败")
            return original_parse_screen(*args, **kwargs)

        app = PickupApp(store, embed_ok=True)
        with (mock.patch("pickup.embed.parse_screen", side_effect=flaky_parse_screen),
              mock.patch("pickup._log_embed_error") as log_error):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                pane = _primary_embed_pane(app.screen)
                await _wait_for_session_name(pane)
                self._hosted_names.append(pane.session_name)
                await _wait_for_pane_text(pane, "CAPTURE-RECOVERY-TEST")

            self.assertGreaterEqual(parse_calls, 2)
            log_error.assert_called_once()

    async def test_focus_shows_real_cursor_blur_hides_it(self) -> None:
        """IME 回归：聚焦内嵌 pane 且有可见光标时必须显式打开外层真实硬件光标
        （`\\e[?25h`），失焦时收起。Textual 全屏运行期默认藏掉真实光标，只移动一个
        看不见的光标——位置再准，IME 也没有可见锚点，用户打不出中文（真机反馈）。
        这里断言 EmbedPane._real_cursor_shown 随焦点/光标状态正确翻转（selftest.sh
        另有真实外层终端 `#{cursor_flag}` 断言，覆盖真正写没写出转义）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'CURSOR-TEST\\n'; cat"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "CURSOR-TEST")
            list_view = app.screen.query_one(SessionListView)
            self.assertTrue(list_view.has_focus)
            self.assertFalse(pane._real_cursor_shown, "列表聚焦时不应显示内嵌真实光标")

            # 回车只挂接画面、不抢焦点；用 Tab 进入右栏（与 selftest 一致）
            await pilot.press("tab")
            await pilot.pause()
            self.assertTrue(pane.has_focus)
            await _wait_until(lambda: pane._real_cursor_shown)
            self.assertTrue(pane._real_cursor_shown, "聚焦活会话时应显示外层真实光标")

            await pilot.press("ctrl+backslash")  # 焦点回列表
            await pilot.pause()
            self.assertFalse(pane._real_cursor_shown, "失焦后应收起外层真实光标")

    async def test_drag_select_then_ctrl_c_copies_to_clipboard(self) -> None:
        """划词选中托管会话画面里的文字后按 Ctrl+C 应复制，不应转发中断信号。

        用的是 Textual 内置的鼠标拖拽文本选择（EmbedPane 没有设 ALLOW_SELECT=
        False），取代旧版 curses 手写的框选高亮 + OSC 52 复制。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", "printf 'HELLO-SELECT-ME\\n'; while true; do sleep 0.1; done"), None
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "HELLO-SELECT-ME")

            await pilot.mouse_down(pane, offset=Offset(0, 0))
            await pilot.hover(pane, offset=Offset(14, 0))
            await pilot.mouse_up(pane, offset=Offset(14, 0))
            await pilot.pause(delay=0.2)
            self.assertEqual(app.screen.get_selected_text(), "HELLO-SELECT-ME")

            await pilot.press("ctrl+c")
            await pilot.pause(delay=0.3)
        self.assertEqual(app._clipboard, "HELLO-SELECT-ME")

    async def test_ctrl_c_without_selection_forwards_interrupt_to_hosted_program(self) -> None:
        """没有选中任何文本时，Ctrl+C 必须原样转发给托管会话（中断当前命令），
        不能被"复制选中文本"这个新功能吞掉——这是终端最基本的操作，回归测试
        钉死（真机排查过 Textual 的按键派发：widget 自己的 on_key 一旦处理并
        stop() 掉事件，BINDINGS 系统根本不会再被咨询，逻辑必须直接写在
        on_key 里，不能指望走 Screen 的 ctrl+c -> copy_text 绑定）。"""
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(
            ("bash", "-c", 'trap "echo GOT-SIGINT" INT; echo READY; while true; do sleep 0.1; done'),
            None,
        )
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(delay=0.5)
            pane = _primary_embed_pane(app.screen)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "READY")

            # 列表仍持有焦点时 Ctrl+C 不会进托管会话；先 Tab 进入右栏再测中断转发
            await pilot.press("tab")
            await pilot.pause()
            self.assertTrue(pane.has_focus)

            await pilot.press("ctrl+c")
            await _wait_for_pane_text(pane, "GOT-SIGINT", tries=30)
            rendered_with_interrupt = pane.render().plain
        self.assertIn("GOT-SIGINT", rendered_with_interrupt)
        # 卸载后的无障碍/测试读取仍应安全返回基础画面，不再访问不存在的 Screen。
        pane.render()

    async def test_host_session_failure_bells_and_stays_in_list(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)

        app = PickupApp(store, embed_ok=True)
        with mock.patch("pickup.embed.host_session", side_effect=__import__("pickup.embed", fromlist=["EmbedError"]).EmbedError("boom")):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                # host_session 现在跑在后台 worker 里，失败结果要经 call_from_thread
                # 回到主线程才会触发 bell；给够时间让这趟线程往返完成。
                await pilot.pause(delay=0.3)
        self.assertIsNone(app.return_value)  # 仍停留在应用内，没有异常退出


class EmbedPaneWheelTests(unittest.TestCase):
    """滚轮转发回归：2026-07-19 卡顿根因——主线程每事件同步 fork 两次 tmux
    （还多发了 xterm 规范里不存在的滚轮 release 序列），触控板惯性滚动把界面堵死。"""

    def test_wheel_forwards_press_only_via_background_sender(self):
        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        pane._mouse_any = True
        with (mock.patch("pickup.embed.send_mouse_sequence") as send_mock,
              mock.patch("pickup.embed.send_literal") as literal_mock):
            pane._wheel(64, 10.0, 5.0, -3)
        # 只发一次 press 序列（64;11;6，坐标 1-based），经后台队列，不直接 fork
        send_mock.assert_called_once_with("pickup-claude-x", "\x1b[<64;11;6M")
        literal_mock.assert_not_called()

    def test_wheel_without_mouse_capture_uses_app_level_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-codex-x"
        pane._mouse_any = False
        pane._scroll = mock.Mock()
        with mock.patch("pickup.embed.send_mouse_sequence") as send_mock:
            pane._wheel(64, 10.0, 5.0, -3)
        pane._scroll.assert_called_once_with(-3)
        send_mock.assert_not_called()

    def test_scroll_handlers_move_app_history_in_expected_direction(self):
        pane = EmbedPane()
        pane.session_name = "pickup-codex-x"
        pane._mouse_any = False
        pane._history_size = 100
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)

        pane._on_mouse_scroll_up(scroll_up)
        self.assertEqual(pane.history_offset, 3)
        pane._on_mouse_scroll_down(scroll_down)
        self.assertEqual(pane.history_offset, 0)

    def test_detail_wheel_follows_document_scroll_direction(self):
        """已结束会话预览：下滚应增大 detail_offset（看更晚内容），与 history_offset 相反。"""
        pane = EmbedPane()
        pane.show_detail(lambda: "预览正文")
        pane.scroll_detail = mock.Mock(return_value=True)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)

        pane._on_mouse_scroll_down(scroll_down)
        pane.scroll_detail.assert_called_with(3)
        pane._on_mouse_scroll_up(scroll_up)
        pane.scroll_detail.assert_called_with(-3)

    def test_show_detail_enables_stick_to_bottom(self):
        """选中静态预览时开启钉底；Home 取消，End 恢复。"""
        pane = EmbedPane()
        pane.show_detail(lambda: "预览正文")
        self.assertTrue(pane._detail_stick_bottom)
        with mock.patch.object(pane, "_is_detail_view", return_value=True), \
             mock.patch.object(pane, "_detail_max_offset", return_value=20), \
             mock.patch.object(pane, "refresh"):
            pane.detail_offset = 20
            pane.scroll_detail_home()
            self.assertFalse(pane._detail_stick_bottom)
            self.assertEqual(pane.detail_offset, 0)
            pane.scroll_detail_end()
            self.assertTrue(pane._detail_stick_bottom)
            self.assertEqual(pane.detail_offset, 20)

    def test_focus_session_without_fallback_stays_blank_canvas(self):
        """无 fallback 时仍空白画布，不出现连接中。"""
        pane = EmbedPane()
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            pane.focus_session("pickup-cursor-new")
        self.assertFalse(pane._detail_stick_bottom)
        self.assertEqual(pane.render().plain, "")
        self.assertNotIn("连接中", pane.render().plain)

    def test_focus_session_with_fallback_enables_stick_bottom(self):
        """有对话回退时 focus_session 必须开启钉底。"""
        pane = EmbedPane()
        with mock.patch("pickup.embed.open_channel", return_value=None), \
             mock.patch("pickup.embed.should_resize_host", return_value=False):
            pane.focus_session("pickup-cursor-x", lambda: "fallback")
        self.assertTrue(pane._detail_stick_bottom)
        self.assertTrue(pane._is_hosted_fallback())
        self.assertTrue(pane._uses_detail_window())

    def test_scroll_handlers_preserve_sgr_direction_without_local_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        pane._mouse_any = True
        pane._history_size = 100
        pane.history_offset = 7
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)

        with mock.patch("pickup.embed.send_mouse_sequence") as send_mock:
            pane._on_mouse_scroll_up(scroll_up)
            pane._on_mouse_scroll_down(scroll_down)

        self.assertEqual(
            send_mock.call_args_list,
            [
                mock.call("pickup-claude-x", "\x1b[<64;11;6M"),
                mock.call("pickup-claude-x", "\x1b[<65;11;6M"),
            ],
        )
        self.assertEqual(pane.history_offset, 7)


class EmbedPaneSelectionSpanTests(unittest.IsolatedAsyncioTestCase):
    """拖选高亮范围回归：全面覆盖纯英文 / 纯中文 / 中英混排 / 全角+半角 /
    多样式段 / 多行（end==-1），穷举每一个字符区间，断言两件事——
    ① 输出文本不丢字（选区边界落在宽字符中间时不能把该字符吃成空格）；
    ② 被高亮的字符正好等于选中的字符（高亮宽度不缩水、不错位）。

    背景（2026-07-20 一连串真机反馈 + 我自己 headless 复现）：Textual 的选区
    坐标系是"字符索引"（`get_span` 返回字符下标），但 `_apply_selection` 早期直接
    把它交给按"cell 列"裁切的 `Strip.crop`。CJK/全角一个字占 2 列，两套坐标不等，
    导致高亮缩水/错位、宽字符被吃成空格。修复：裁切前用 `cell_len(text[:idx])`
    把字符索引换算成 cell 列。此前只针对纯中文写过一个窄测试就发版，漏了英文/
    混排——这个类就是补齐"充分的测试用例设计"。"""

    def _spans_ok(self, pane, strip, text):
        """穷举 [s,e) 字符区间，逐个断言不丢字、高亮精确。strip 已带 offset 元数据。"""
        from unittest.mock import PropertyMock
        from textual.selection import Selection
        from textual.geometry import Offset

        n = len(text)
        for s in range(n + 1):
            for e in range(s, n + 1):
                sel = Selection(Offset(s, 0), Offset(e, 0))
                with mock.patch.object(
                    EmbedPane, "text_selection",
                    new_callable=PropertyMock, return_value=sel,
                ):
                    out = pane._apply_selection(strip, 0)
                self.assertEqual(out.text, strip.text, f"{text!r} 选区[{s}:{e}] 丢字了")
                highlighted = "".join(
                    seg.text for seg in out if seg.style and seg.style.bgcolor
                )
                self.assertEqual(highlighted, text[s:e], f"{text!r} 选区[{s}:{e}] 高亮错位")

    async def test_selection_spans_across_scripts(self):
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()

            cases = [
                "hello world",          # 纯英文（半角）
                "另外提醒一件事",         # 纯中文（全角）
                "标点bug hello",         # 中前英后
                "hi 你好 bye",           # 英-中-英
                "ａｂｃabc你好x",         # 全角字母 + 半角字母 + 中文 + 半角
            ]
            for text in cases:
                # 单段
                single = Strip([Segment(text, Style(color="#e0e0e0"))]).apply_offsets(0, 0)
                self._spans_ok(pane, single, text)
                # 多段（每 3 个字符换一种颜色，模拟真实语法高亮）
                segs, i, palette = [], 0, ["#ff0000", "#00ff00", "#0088ff"]
                while i < len(text):
                    segs.append(Segment(text[i:i + 3], Style(color=palette[(i // 3) % 3])))
                    i += 3
                multi = Strip(segs).apply_offsets(0, 0)
                self._spans_ok(pane, multi, text)

    async def test_selection_to_end_of_line_uses_full_width(self):
        """多行选区里，非末行的 get_span 返回 (start, -1)（一直选到行尾）；
        end==-1 必须换算成整行 cell 宽度，且中英文都不能丢字。"""
        from unittest.mock import PropertyMock
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip
        from textual.selection import Selection
        from textual.geometry import Offset

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            for text in ["hello world", "另外a提b醒", "ｍｉｘ 混排 end"]:
                strip = Strip([Segment(text, Style(color="#e0e0e0"))]).apply_offsets(0, 0)
                for start in range(len(text) + 1):
                    sel = Selection(Offset(start, 0), None)  # None end -> get_span 给 (start,-1)
                    with mock.patch.object(
                        EmbedPane, "text_selection",
                        new_callable=PropertyMock, return_value=sel,
                    ):
                        out = pane._apply_selection(strip, 0)
                    self.assertEqual(out.text, strip.text, f"{text!r} 选到行尾[{start}:] 丢字了")
                    highlighted = "".join(
                        seg.text for seg in out if seg.style and seg.style.bgcolor
                    )
                    self.assertEqual(highlighted, text[start:], f"{text!r} 选到行尾[{start}:] 高亮错")

    async def test_selection_through_real_parse_pipeline(self):
        """走真实解析管线（parse_screen -> _row_to_strip -> adjust_cell_length ->
        apply_offsets），端到端验证 render_line 的选区渲染，覆盖英文与混排。"""
        from unittest.mock import PropertyMock
        from textual.selection import Selection
        from textual.geometry import Offset
        from textual.geometry import Size
        from rich.cells import cell_len
        from pickup import embed
        from pickup.ui.embed_pane import _row_to_strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        WIDTH = 40
        async with app.run_test(size=(80, 24)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            pane.session_name = "pickup-claude-x"
            pane.dead = False
            for line in ["the quick brown fox", "标点bug hello", "ｈｅｌｌｏ ab 你好"]:
                grid = embed.parse_screen(line, width=WIDTH, height=1)
                pane._grid = grid
                pane._strips = [_row_to_strip(grid[0])]
                n = len(line)
                for s in range(n + 1):
                    for e in range(s, n + 1):
                        sel = Selection(Offset(s, 0), Offset(e, 0))
                        with mock.patch.object(
                            EmbedPane, "text_selection",
                            new_callable=PropertyMock, return_value=sel,
                        ), mock.patch.object(
                            type(pane), "size",
                            new_callable=PropertyMock, return_value=Size(WIDTH, 24),
                        ):
                            out = pane.render_line(0)
                        # render_line 会把行补齐到面板宽度，取前 n 个可见字符比对
                        self.assertTrue(out.text.startswith(line), f"{line!r}[{s}:{e}] 可见文本被破坏")
                        highlighted = "".join(
                            seg.text for seg in out if seg.style and seg.style.bgcolor
                        )
                        self.assertEqual(highlighted, line[s:e], f"{line!r} 选区[{s}:{e}] 高亮错位")

    async def test_active_selection_does_not_corrupt_offset_metadata(self):
        """拖选进行中不能污染 offset 元数据——这是"从中间往右拖、起点左边反而
        被高亮"的真正根因（2026-07-20 真机反馈，中英文都中招）。

        Textual 在拖选过程中会反复回读 render_line 把屏幕列换算成字符位置。若
        render_line 先 apply_offsets 再 crop 出选区三段，`Strip.crop` 拆 Segment
        时只照抄原 offset 不重算，三段会全带原整段的 offset（单段行拆完三段都
        是 (0,0)），换算就崩到行首——选区反向。正确顺序是先 crop 再 apply_offsets。

        不变量：不论当前有没有选区，render_line 出来的每个 Segment 的 offset 起
        始下标都必须等于它前面所有 Segment 文本的累计字符数（即与"对同一文本重新
        apply_offsets"完全一致）。"""
        from unittest.mock import PropertyMock
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip
        from textual.selection import Selection
        from textual.geometry import Offset, Size

        def offsets_consistent(strip):
            expect = 0
            for seg in strip:
                meta = seg.style.meta.get("offset") if (seg.style and seg.style._meta) else None
                if meta is None or meta[0] != expect:
                    return False, expect, meta
                expect += len(seg.text)
            return True, None, None

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(60, 8)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()
            pane.session_name = "x"
            pane.dead = False
            for text in ["the quick brown fox", "另外提醒一件事abc", "标点bug hello"]:
                W = max(1, len(text))
                pane._grid = [[object()] * len(text)]
                pane._strips = [Strip([Segment(text, Style(color="#e0e0e0"))])]
                n = len(text)
                # 模拟拖选进行中：从每个可能的起点选出一小段
                for anchor in range(n + 1):
                    sel = Selection(Offset(anchor, 0), Offset(min(anchor + 1, n), 0))
                    with mock.patch.object(
                        EmbedPane, "text_selection",
                        new_callable=PropertyMock, return_value=sel,
                    ), mock.patch.object(
                        type(pane), "size",
                        new_callable=PropertyMock, return_value=Size(W, 8),
                    ):
                        out = pane.render_line(0)
                    ok, expect, meta = offsets_consistent(out)
                    self.assertTrue(
                        ok,
                        f"{text!r} anchor={anchor}: 选区把 offset 元数据搞乱了，"
                        f"某段应起于字符 {expect} 实际 offset={meta}",
                    )


class EmbedPaneSelectionStyleTests(unittest.IsolatedAsyncioTestCase):
    """拖选高亮不能盖住文字：2026-07-20 真机反馈"高亮把选中的文字整个遮住看
    不见"。headless 启动真实 app 打印证据确认——Textual 默认
    screen-selection-foreground 是 transparent（保留原前景语义），但
    get_component_rich_style 会把它预解析成一个具体色且恰好等于选区背景色，
    整段套上去后前景==背景，文字隐形。修复：前景 transparent 时只染背景、
    保留每个 Segment 原本的前景色。"""

    async def test_selection_style_preserves_foreground(self) -> None:
        from rich.segment import Segment
        from rich.style import Style
        from textual.strip import Strip

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(120, 30)) as pilot:
            pane = EmbedPane()
            await app.screen.mount(pane)
            await pilot.pause()

            sel = pane._selection_style()
            # 关键断言：选区样式不覆盖前景（保留原文字色），但要有背景色
            self.assertIsNone(sel.color, "选区前景应留空以保留原文字色，否则会盖住文字")
            self.assertIsNotNone(sel.bgcolor, "选区必须有背景色才能体现选中")

            # 端到端：一段有明确前景的文字套上选区样式后，前景必须原样保留、
            # 且不等于背景（等于就等于看不见）
            base = Strip([Segment("Hello 中文", Style(color="#e0e0e0"))])
            highlighted = base.crop(0, base.cell_length).apply_style(sel)
            seg = list(highlighted)[0]
            self.assertEqual(seg.style.color.triplet.hex, "#e0e0e0")
            self.assertNotEqual(seg.style.color, seg.style.bgcolor)


class EmbedPaneResizeTests(unittest.IsolatedAsyncioTestCase):
    """窗口缩放：行宽即时裁补；tmux resize + 抓帧必须防抖，不能拖动期狂刷。"""

    def test_render_line_adjusts_cached_strip_to_current_width(self) -> None:
        from rich.segment import Segment
        from textual.strip import Strip

        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        # 模拟旧宽度缓存行（10 列），面板已缩到 6 列
        pane._grid = [[object()] * 10]  # 非空即可让 render_line 走 _strips 分支
        pane._strips = [Strip([Segment("abcdefghij")])]
        with mock.patch.object(type(pane), "size", new_callable=mock.PropertyMock) as size_mock:
            size_mock.return_value = Size(6, 1)
            strip = pane.render_line(0)
        self.assertEqual(strip.cell_length, 6)
        self.assertEqual(strip.text, "abcdef")

    async def test_tmux_resize_and_capture_are_debounced(self) -> None:
        import pickup.ui.embed_pane as embed_pane_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-claude-debounce"
            pane.dead = False
            resize_calls: list[tuple] = []
            poke_calls: list[int] = []

            def _fake_resize(name, w, h):
                resize_calls.append((name, w, h))

            with (
                mock.patch("pickup.embed.resize", side_effect=_fake_resize),
                mock.patch.object(pane._poke, "set", side_effect=lambda: poke_calls.append(1)),
            ):
                pane._on_resize(events.Resize(Size(50, 20), Size(50, 20)))
                await pilot.pause(delay=0.02)
                pane._on_resize(events.Resize(Size(40, 18), Size(40, 18)))
                await pilot.pause(delay=0.02)
                self.assertEqual(resize_calls, [], "拖动过程中不应立刻 resize-window")
                self.assertEqual(poke_calls, [], "拖动过程中不应立刻唤醒抓帧")
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
                self.assertEqual(resize_calls, [("pickup-claude-debounce", 40, 18)])
                self.assertEqual(len(poke_calls), 1)

    async def test_resize_with_live_grid_starts_capture_hold(self) -> None:
        """已有直播画面时，防抖 resize 后必须冻结抓帧显示，避免镜像 Cursor 重排滚动。"""
        import pickup.ui.embed_pane as embed_pane_mod
        from pickup.embed import Cell

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-cursor-hold"
            pane.dead = False
            pane._grid = [[Cell("x")]]  # noqa: SLF001
            with (
                mock.patch("pickup.embed.resize"),
                mock.patch("pickup.embed.capture", return_value=None),
            ):
                pane._on_resize(events.Resize(Size(60, 22), Size(60, 22)))
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
            self.assertTrue(pane._resize_hold_active)  # noqa: SLF001
            # 停掉抓帧线程对 hold 状态的并发改写，再单测放行条件
            pane.session_name = None
            pane._stop.set()  # noqa: SLF001
            # 重排中的变化帧不得放行
            self.assertFalse(pane._resize_hold_allows_display("frame-a"))  # noqa: SLF001
            self.assertFalse(pane._resize_hold_allows_display("frame-b"))  # noqa: SLF001
            # 等到最小 hold 之后，连续两帧相同才放行
            pane._resize_hold_until_min = time.monotonic() - 0.01  # noqa: SLF001
            self.assertFalse(pane._resize_hold_allows_display("stable"))  # noqa: SLF001
            self.assertTrue(pane._resize_hold_allows_display("stable"))  # noqa: SLF001
            self.assertFalse(pane._resize_hold_active)  # noqa: SLF001

    def test_resize_hold_deadline_forces_release(self) -> None:
        """超时后即使画面仍在变也必须放行，避免永久冻结。"""
        pane = EmbedPane()
        pane._begin_resize_capture_hold()  # noqa: SLF001
        pane._resize_hold_until_min = time.monotonic() - 1  # noqa: SLF001
        pane._resize_hold_deadline = time.monotonic() - 0.01  # noqa: SLF001
        self.assertTrue(pane._resize_hold_allows_display("still-changing"))  # noqa: SLF001
        self.assertFalse(pane._resize_hold_active)  # noqa: SLF001

    async def test_tmux_resize_skips_when_pane_too_narrow(self) -> None:
        """右栏短时缩到下限以下时不得 resize-window，避免窄折行烧进历史。"""
        import pickup.ui.embed_pane as embed_pane_mod

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pane = _primary_embed_pane(app.screen)
            pane.session_name = "pickup-claude-narrow"
            pane.dead = False
            resize_calls: list[tuple] = []

            with mock.patch("pickup.embed.resize", side_effect=lambda *a: resize_calls.append(a)):
                pane._on_resize(events.Resize(Size(20, 18), Size(20, 18)))
                await pilot.pause(delay=embed_pane_mod._RESIZE_TMUX_DEBOUNCE + 0.05)
                self.assertEqual(resize_calls, [])


@unittest.skipUnless(HAS_TMUX, "内嵌面板依赖真实 tmux")
class DirectLaunchHostingTests(unittest.IsolatedAsyncioTestCase):
    """直启子命令（pickup claude ...）带进 TUI 的托管路径。"""

    def setUp(self) -> None:
        self._hosted_names: list[str] = []
        self.addCleanup(self._cleanup_hosted)

    def _cleanup_hosted(self) -> None:
        for name in self._hosted_names:
            subprocess.run(["tmux", "-L", "pickup-keepalive", "kill-session", "-t", name],
                            stderr=subprocess.DEVNULL)

    async def test_direct_launch_hosts_and_focuses_pane_without_stealing_focus_back(self) -> None:
        """直启托管成功后焦点应在右栏；且挂载时不能再调度列表 focus 把焦点抢回去。"""
        store, _ = _make_store()
        plan = LaunchPlan(("bash", "-c", "printf 'DIRECT-HELLO\\n'; cat"), None)
        direct = pickup._DirectLaunch(plan, "claude", "directtest01")

        app = PickupApp(store, embed_ok=True, direct=direct)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            pane = _primary_embed_pane(app.screen)
            # embed.host_session 现在跑在后台 worker 里（见 _host_direct_worker），
            # 不再保证固定延迟内一定完成，轮询等待比死等更稳。
            await _wait_for_session_name(pane)
            self.assertIsNotNone(pane.session_name)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "DIRECT-HELLO")
            self.assertTrue(pane.has_focus)

            await _wait_until(lambda: store.find_session("claude:directtest01") is not None)
            provisional = store.find_session("claude:directtest01")
            self.assertIsNotNone(provisional)
            self.assertTrue(provisional["provisional"])
            self.assertTrue(provisional["live"])
            self.assertEqual(provisional["keepalive_name"], pane.session_name)
            self.assertEqual(provisional["fallback_title"], "新Claude会话")
            self.assertEqual(provisional["cwd"], os.getcwd())
            self.assertIn(
                "claude:directtest01",
                [pickup.session_key(session) for session in store.all_sessions()],
            )

            await pilot.press(*"x")
            await _wait_for_pane_text(pane, "x")
            self.assertIn("x", pane.render().plain.split("DIRECT-HELLO")[-1])


class RightPanePreviewTests(unittest.IsolatedAsyncioTestCase):
    """选中即完整预览：右栏展示对话全文。"""

    async def test_right_pane_shows_full_conversation_not_last_qa_blurb(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            pane = _primary_embed_pane(app.screen)

            await _wait_until(lambda: "测试问题" in pane.render().plain and "测试回复" in pane.render().plain)
            detail = pane.render().plain
            self.assertIn("● You", detail)
            self.assertIn("测试问题", detail)
            self.assertIn("测试回复", detail)
            self.assertNotIn("最近提问", detail)
            self.assertNotIn("最近回复", detail)

    async def test_space_no_longer_opens_fullscreen_preview(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            self.assertIs(app.screen, app.screen)
            self.assertEqual(type(app.screen).__name__, "MainScreen")

    async def test_a_key_opens_handoff_modal_from_main_list(self) -> None:
        codex = mock.Mock()
        codex.id = "codex"
        codex.display_name = "Codex"
        codex.is_available.return_value = True
        codex.scan_sessions.return_value = []
        store, _ = _make_store(extra_runtimes=(codex,))
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.press("down")  # claude(原生恢复) -> codex
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)
        self.assertEqual(app.return_value.target_runtime_id, "codex")

    async def test_right_pane_detail_scrolls_with_page_and_end(self) -> None:
        """长对话预览：默认钉底；PgUp / Home 可回到更早内容；End 再回最新。"""
        long_msgs = []
        for i in range(40):
            long_msgs.append(pickup.ConversationMessage("user", f"问题行-{i}-" + ("x" * 20)))
            long_msgs.append(pickup.ConversationMessage("assistant", f"回复行-{i}-" + ("y" * 20)))
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "长对话",
            "cwd": "/tmp", "live": False,
        }]
        store, registry = _make_store(sessions=sessions)
        registry.get("claude").load_conversation.return_value = long_msgs
        # 清掉 store.load() 时预热的短对话缓存，强制按新返回值重读
        store.conversations.clear()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            pane = _primary_embed_pane(app.screen)
            await _wait_until(
                lambda: (
                    pane._is_detail_view()
                    and "回复行-39" in pane.render().plain
                    and pane.detail_offset == pane._detail_max_offset() > 0
                ),
                tries=300,
                interval=0.02,
            )
            # 直接调滚动 API 验证窗口机制（再测键盘绑定）
            self.assertTrue(pane.scroll_detail_page(-1))
            self.assertLess(pane.detail_offset, pane._detail_max_offset())
            after_page_up = pane.detail_offset
            await pilot.press("home")
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)
            self.assertIn("问题行-0", pane.render().plain)
            # 用户已离开底部后，invalidate 不得强行钉回底部
            pane.invalidate_detail()
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)
            await pilot.press("end")
            await pilot.pause()
            self.assertEqual(pane.detail_offset, pane._detail_max_offset())
            self.assertGreater(pane.detail_offset, after_page_up)
            self.assertIn("回复行-39", pane.render().plain)

    async def test_detail_async_load_pins_to_bottom(self) -> None:
        """对话异步填入后仍应钉在最新；用户上滚后刷新保持当前位置。"""
        long_msgs = [
            pickup.ConversationMessage("user", f"早-{i}-" + ("u" * 40))
            for i in range(30)
        ] + [
            pickup.ConversationMessage("assistant", "最新答复-" + ("z" * 40)),
        ]
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "异步预览",
            "cwd": "/tmp", "live": False,
        }]
        store, registry = _make_store(sessions=sessions)
        # 首次 peek 为空：模拟暖加载前；随后 get_conversation 写入缓存
        registry.get("claude").load_conversation.return_value = long_msgs
        store.conversations.clear()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            pane = _primary_embed_pane(app.screen)
            await _wait_until(
                lambda: pane._is_detail_view() and "最新答复" in pane.render().plain,
                tries=300,
                interval=0.02,
            )
            self.assertEqual(pane.detail_offset, pane._detail_max_offset())
            self.assertTrue(pane.scroll_detail_home())
            self.assertEqual(pane.detail_offset, 0)
            # 模拟后台刷新（暖加载完成再次 invalidate）
            app.screen._refresh_preview_detail()
            await pilot.pause()
            self.assertEqual(pane.detail_offset, 0)


class ModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_menu_modal_escape_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    PickMenuModal("标题", [("甲", "提示甲"), ("乙", "提示乙")])
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            await pilot.press("escape")
            await pilot.pause(delay=0.2)
        self.assertIsNone(result_holder.get("result"))

    async def test_runtime_picker_modal_bells_on_unavailable_choice(self) -> None:
        kimi = mock.Mock()
        kimi.id = "kimi"
        kimi.display_name = "Kimi"
        kimi.is_available.return_value = False
        kimi.scan_sessions.return_value = []
        store, _ = _make_store(extra_runtimes=(kimi,))
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.press("down")  # 移到未安装的 kimi
            bell_calls_before = app._bell_count if hasattr(app, "_bell_count") else None
            with mock.patch.object(app, "bell") as bell:
                await pilot.press("enter")
                await pilot.pause()
            bell.assert_called_once()
            self.assertIsInstance(app.screen, RuntimePickerModal)  # 未安装项不应关闭弹窗

    async def test_confirm_modal_other_key_cancels(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(ConfirmModal("确认？"))

            app.run_worker(_open())
            await pilot.pause(delay=0.2)
            await pilot.press("n")
            await pilot.pause(delay=0.2)
        self.assertFalse(result_holder.get("result"))

    async def test_confirm_modal_q_confirms(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(ConfirmModal("确认？"))

            app.run_worker(_open())
            await pilot.pause(delay=0.3)  # 等 ConfirmModal call_after_refresh 武装
            await pilot.press("q")
            await pilot.pause(delay=0.2)
        self.assertTrue(result_holder.get("result"))

    async def test_confirm_modal_custom_key_confirms_and_q_no_longer_does(self) -> None:
        """删除会话复用 ConfirmModal 但确认键换成 x；默认键 q 此时不应再生效。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    ConfirmModal("删除？", confirm_key="x")
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.3)
            await pilot.press("q")  # 不再是确认键，应按取消处理
            await pilot.pause(delay=0.2)
        self.assertFalse(result_holder.get("result"))

    async def test_confirm_modal_custom_key_x_confirms(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            result_holder = {}

            async def _open():
                result_holder["result"] = await app.push_screen_wait(
                    ConfirmModal("删除？", confirm_key="x")
                )

            app.run_worker(_open())
            await pilot.pause(delay=0.3)
            await pilot.press("x")
            await pilot.pause(delay=0.2)
        self.assertTrue(result_holder.get("result"))


class KillKeepaliveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_key_confirm_kills_and_clears_keepalive_name(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242, "keepalive_name": "pickup-claude-fake",
        }]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        with mock.patch("pickup.keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                list_view = app.screen.query_one(SessionListView)
                card = list_view._session_cards()[0]
                self.assertIn("Running (hosted)", card.render().plain)
                await pilot.press("q")
                await pilot.pause(delay=0.3)  # worker 推弹窗 + ConfirmModal 武装
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("q")
                await pilot.pause(delay=0.2)
                # 确认后立刻应是已结束，不能先闪一帧「运行中」（live 仍为 True）
                card = list_view._session_cards()[0]
                plain = card.render().plain
                self.assertIn("Ended", plain)
                self.assertNotIn("Running", plain)
        kill_mock.assert_called_once_with("pickup-claude-fake")
        current = store.find_session("claude:s0")
        self.assertIsNotNone(current)
        self.assertNotIn("keepalive_name", current)
        self.assertFalse(current.get("live"))
        self.assertIsNone(current.get("pid"))


class DeleteSessionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_x_key_confirm_deletes_ended_session_and_removes_card(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)  # worker 推弹窗 + ConfirmModal 武装
            self.assertIsInstance(app.screen, ConfirmModal)
            await pilot.press("x")
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertEqual(list_view._session_cards(), [])
        claude_runtime.delete_session.assert_called_once_with(sessions[0])
        self.assertIsNone(store.find_session("claude:s0"))

    async def test_x_key_other_key_cancels_and_keeps_session(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("n")  # 非确认键，取消
            await pilot.pause(delay=0.2)
        claude_runtime.delete_session.assert_not_called()
        self.assertIsNotNone(store.find_session("claude:s0"))

    async def test_x_key_on_running_session_kills_keepalive_then_deletes(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242, "keepalive_name": "pickup-claude-fake",
            "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        app = PickupApp(store, embed_ok=False)
        with mock.patch("pickup.keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("x")
                await pilot.pause(delay=0.3)
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("x")
                await _wait_until(
                    lambda: not isinstance(app.screen, ConfirmModal)
                    and not app.screen.query_one(SessionListView)._session_cards()
                )
                list_view = app.screen.query_one(SessionListView)
                self.assertEqual(list_view._session_cards(), [])
        kill_mock.assert_called_once_with("pickup-claude-fake")
        claude_runtime.delete_session.assert_called_once()
        self.assertIsNone(store.find_session("claude:s0"))

    async def test_delete_failure_keeps_card_and_notifies(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "path": "/tmp/s0.jsonl",
        }]
        store, registry = _make_store(sessions=sessions)
        claude_runtime = registry.get("claude")
        claude_runtime.delete_session.side_effect = OSError("模拟磁盘删除失败")
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("x")
            await pilot.pause(delay=0.3)
            await pilot.press("x")
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            self.assertEqual(len(list_view._session_cards()), 1)
        self.assertIsNotNone(store.find_session("claude:s0"))


if __name__ == "__main__":
    unittest.main()
