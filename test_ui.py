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

import i18n

# 界面测试固定英文，避免 CI/本机 LANG=zh* 时断言漂移
i18n.set_lang("en")

import pickup
from models import LaunchPlan
from textual import events
from textual.color import Color
from textual.geometry import Offset, Size
from textual.widgets import Input, ListItem
from ui.app import PickupApp
from ui.embed_pane import EmbedPane
from ui.modals import ConfirmModal, PickMenuModal, RuntimePickerModal
from ui.session_list import NEW_SESSION_ID, SessionCard, SessionListView

HAS_TMUX = shutil.which("tmux") is not None


async def _wait_until(predicate, *, tries: int = 100, interval: float = 0.01) -> None:
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
            self.assertEqual(app.theme, "textual-light")

    async def test_dark_background_uses_dark_theme(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "textual-dark")

    async def test_missing_report_falls_back_to_default_dark(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False, osc_report=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertEqual(app.theme, "textual-dark")

    async def test_f12_saves_screenshot_under_cache(self) -> None:
        import observe
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
        from ui.embed_pane import EmbedPane

        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True, osc_report=b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            pane = app.screen.query_one(EmbedPane)
            self.assertEqual(pane.styles.background.rgb, (0x1e, 0x1e, 0x2e))


class MainScreenWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """主屏退出时不能被长驻刷新线程或首屏等待线程拖住。"""

    async def test_background_refresh_worker_is_cancelled_on_normal_exit(self) -> None:
        store, _ = _make_store()
        store.refresh = mock.Mock(return_value=False)
        app = PickupApp(store, embed_ok=False)

        started_at = time.monotonic()
        with (
            mock.patch("ui.main_screen.REFRESH_INTERVAL", 0.01),
            mock.patch("ui.main_screen.REFRESH_INTERVAL_MAX", 0.02),
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

    def test_runtime_is_right_aligned_on_first_line_at_fixed_width(self) -> None:
        card = self._card()
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 2),
        ):
            rendered = card.render()

        first_line, second_line = rendered.plain.splitlines()
        self.assertTrue(first_line.endswith("OpenCode"))
        self.assertNotIn("OpenCode", second_line)
        self.assertEqual(pickup._text_width(first_line), 39)
        self.assertEqual(pickup._text_width(second_line), 39)
        relative_time = pickup._format_relative_time(card.session["mtime"])
        self.assertTrue(second_line.endswith(relative_time))

    def test_long_title_uses_ellipsis_before_runtime_name(self) -> None:
        card = self._card(
            display_title="这是一个非常非常非常长的侧边栏标题用来验证省略",
        )
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 2),
        ):
            rendered = card.render()

        first_line = rendered.plain.splitlines()[0]
        self.assertTrue(first_line.endswith("OpenCode"))
        title_part = first_line[: -len("OpenCode")]
        self.assertIn("...", title_part)
        self.assertEqual(pickup._text_width(first_line), 39)

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
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 2),
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

    def test_generating_title_keeps_spinner_without_bold(self) -> None:
        card = self._card(generating=True)
        with mock.patch.object(
            SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 2),
        ):
            rendered = card.render()

        first_line = rendered.plain.splitlines()[0]
        runtime_start = first_line.rfind("OpenCode")
        self.assertTrue(rendered.plain.startswith("◐ "))
        # 只断言标题段不加粗；右上角 runtime 名允许 bold + 配色。
        title_spans = [
            span for span in rendered.spans
            if span.start < runtime_start and span.end <= runtime_start
        ]
        self.assertFalse(any("bold" in str(span.style) for span in title_spans))

    def test_running_status_is_green_but_ended_status_is_not(self) -> None:
        cases = (
            (self._card(live=True), "Running", True),
            (self._card(keepalive_name="pickup-opencode-visual"), "Running (hosted)", True),
            (self._card(), "Ended", False),
        )
        for card, status_text, expected_green in cases:
            with self.subTest(status=status_text), mock.patch.object(
                SessionCard, "size", new_callable=mock.PropertyMock, return_value=Size(39, 2),
            ):
                rendered = card.render()

            status_start = rendered.plain.index("\n") + 1
            status_end = status_start + len(status_text)
            green_spans = [
                span for span in rendered.spans
                if "green" in str(span.style)
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

            self.assertEqual(search.styles.color, Color.parse("white"))
            self.assertEqual(search.region.height, 2)  # 正文 1 + 末行间隔 1
            self.assertGreaterEqual(len(items), 3)
            # 分隔空行画在卡片自身内，两项 region 紧挨、无外边距空隙
            self.assertEqual(items[1].region.y - items[0].region.bottom, 0)
            self.assertEqual(items[2].region.y - items[1].region.bottom, 0)
            self.assertTrue(cards)
            self.assertTrue(all(card.region.height == 3 for card in cards))
            from ui.session_list import NewSessionCard
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
        """卡片底部分隔空行画在 SessionCard/NewSessionCard 自身高度内，
        点在空行上仍选中该会话（不要用 ListItem margin/padding 做分隔）。"""
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        self.assertNotIn("margin-bottom:", PickupApp.CSS)
        self.assertNotIn("padding-bottom:", PickupApp.CSS)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            card = app.screen.query(SessionCard).first()
            self.assertEqual(card.region.height, 3)
            # 点第三行空行，应等价于点该会话卡
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
            pane = screen.query_one(EmbedPane)
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

    async def test_e_key_forces_fullscreen_even_when_embed_available(self) -> None:
        """`e` 是「放弃分栏、全屏接管」的逃生舱：即使内嵌可用也必须直接退出应用。"""
        store, registry = _make_store()

        def fake_build_launch_plan(request):
            return LaunchPlan(("claude",), None)
        registry.build_launch_plan = fake_build_launch_plan

        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("e")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)


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
        with mock.patch("embed.host_session", side_effect=delayed_host) as host_mock:
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                pane = app.screen.query_one(EmbedPane)
                pane.focus_session = mock.Mock()
                old_request_session = store.sessions["claude"][0]

                await pilot.press("down")
                await pilot.press("enter")
                await _wait_until(started.is_set)
                self.assertTrue(app.screen._host_busy)

                # 第一次托管还没结束时重复确认，只响铃，不应再启动第二个进程。
                await pilot.press("enter")
                await pilot.pause(delay=0.05)
                self.assertEqual(host_mock.call_count, 1)

                current_session = dict(old_request_session, mtime=old_request_session["mtime"] + 1)
                store.sessions["claude"][0] = current_session
                release.set()
                await _wait_until(lambda: not app.screen._host_busy)

                self.assertEqual(host_mock.call_count, 1)
                self.assertEqual(current_session.get("keepalive_name"), "pickup-claude-s0")
                self.assertNotIn("keepalive_name", old_request_session)
                self.assertTrue(pane.focus_session.called)
                self.assertEqual(pane.focus_session.call_args_list[0].args[0], "pickup-claude-s0")

    async def test_host_failure_releases_single_flight_guard(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("embed.host_session", side_effect=RuntimeError("模拟启动失败")) as host_mock,
            mock.patch.object(pickup, "_log_embed_error"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                await _wait_until(lambda: host_mock.call_count == 1)
                await _wait_until(lambda: not app.screen._host_busy)
                self.assertFalse(app.screen._host_busy)

    async def test_new_session_shortcut_hosts_without_reading_session(self) -> None:
        """回归：按 n 走 NewSessionRequest 时，托管成功回调不得访问 request.session。"""
        store, registry = _make_store()
        registry.build_new_session_plan = lambda request: LaunchPlan(("claude",), "/tmp")
        app = PickupApp(store, embed_ok=True)

        with (
            mock.patch("embed.host_session", return_value="pickup-claude-new"),
            mock.patch.object(pickup, "usable_cwd", return_value="/tmp"),
            mock.patch.object(pickup, "_new_session_cwd", return_value="/tmp"),
        ):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                pane = app.screen.query_one(EmbedPane)
                pane.focus_session = mock.Mock()
                await pilot.press("n")
                await _wait_until(lambda: not app.screen._host_busy)
                self.assertTrue(pane.focus_session.called)
                self.assertEqual(pane.focus_session.call_args_list[0].args[0], "pickup-claude-new")
                # 新建路径没有关联会话，fallback 必须是 None
                self.assertIsNone(pane.focus_session.call_args_list[0].args[1])


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
        with mock.patch("embed.open_channel", return_value=None):
            async with app.run_test(size=(120, 30)):
                pane = app.screen.query_one(EmbedPane)

                pane.focus_session("已有会话", lambda: "即时会话详情")
                self.assertEqual(pane.render().plain, "即时会话详情")

                pane.focus_session("刚启动的新会话")
                self.assertEqual(pane.render().plain, "")
                self.assertNotIn("连接中", pane.render().plain)

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
            await pilot.pause(delay=0.2)
            pane = app.screen.query_one(EmbedPane)
            await _wait_for_session_name(pane)
            self._hosted_names.append(pane.session_name)
            self.assertNotIn("连接中", pane.render().plain)
            await _wait_for_pane_text(pane, "HELLO-UI-TEST")
            self.assertTrue(pane.has_focus)

            # ctrl+backslash 回列表，'c' 关闭分栏；托管会话应在后台继续存活
            await pilot.press("ctrl+backslash")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            self.assertIsNone(pane.session_name)

        import embed
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
            pane = app.screen.query_one(EmbedPane)
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
            pane = app.screen.query_one(EmbedPane)
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
            pane = app.screen.query_one(EmbedPane)
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
        import embed

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
        with (mock.patch("embed.parse_screen", side_effect=flaky_parse_screen),
              mock.patch("pickup._log_embed_error") as log_error):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                pane = app.screen.query_one(EmbedPane)
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
            pane = app.screen.query_one(EmbedPane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "CURSOR-TEST")
            self.assertTrue(pane.has_focus)
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
            pane = app.screen.query_one(EmbedPane)
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
            pane = app.screen.query_one(EmbedPane)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "READY")

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
        with mock.patch("embed.host_session", side_effect=__import__("embed").EmbedError("boom")):
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
        with (mock.patch("embed.send_mouse_sequence") as send_mock,
              mock.patch("embed.send_literal") as literal_mock):
            pane._wheel(64, 10.0, 5.0, -3)
        # 只发一次 press 序列（64;11;6，坐标 1-based），经后台队列，不直接 fork
        send_mock.assert_called_once_with("pickup-claude-x", "\x1b[<64;11;6M")
        literal_mock.assert_not_called()

    def test_wheel_without_mouse_capture_uses_app_level_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-codex-x"
        pane._mouse_any = False
        pane._scroll = mock.Mock()
        with mock.patch("embed.send_mouse_sequence") as send_mock:
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

    def test_scroll_handlers_preserve_sgr_direction_without_local_scroll(self):
        pane = EmbedPane()
        pane.session_name = "pickup-claude-x"
        pane._mouse_any = True
        pane._history_size = 100
        pane.history_offset = 7
        scroll_up = events.MouseScrollUp(None, 10, 5, 0, 0, 0, False, False, False)
        scroll_down = events.MouseScrollDown(None, 10, 5, 0, 0, 0, False, False, False)

        with mock.patch("embed.send_mouse_sequence") as send_mock:
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
        """回归测试：MainScreen.on_mount 曾经先调度 SessionListView.focus()（延迟生效），
        直启托管完成、显式聚焦面板之后，那个延迟的列表 focus() 会在下一轮事件循环
        把焦点抢回列表——键盘输入实际发不到内嵌会话（真机 tmux 冒烟才暴露的 bug，
        修法是直启场景完全不调用列表的 focus()）。"""
        store, _ = _make_store()
        plan = LaunchPlan(("bash", "-c", "printf 'DIRECT-HELLO\\n'; cat"), None)
        direct = pickup._DirectLaunch(plan, "claude", "directtest01")

        app = PickupApp(store, embed_ok=True, direct=direct)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            pane = app.screen.query_one(EmbedPane)
            # embed.host_session 现在跑在后台 worker 里（见 _host_direct_worker），
            # 不再保证固定延迟内一定完成，轮询等待比死等更稳。
            await _wait_for_session_name(pane)
            self.assertIsNotNone(pane.session_name)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "DIRECT-HELLO")
            self.assertTrue(pane.has_focus)

            # 键盘输入必须真的到达托管会话，而不是被抢回焦点的列表吞掉
            await pilot.press(*"x")
            await _wait_for_pane_text(pane, "x")
            self.assertIn("x", pane.render().plain.split("DIRECT-HELLO")[-1])


class RightPanePreviewTests(unittest.IsolatedAsyncioTestCase):
    """选中即完整预览：右栏展示对话全文；Space 全屏预览已退役。"""

    async def test_right_pane_shows_full_conversation_not_last_qa_blurb(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.pause(delay=0.3)
            pane = app.screen.query_one(EmbedPane)

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
        """长对话预览：PgDn / End 必须真正改变可见窗口（此前静态详情被裁成一屏无法滚动）。"""
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
            pane = app.screen.query_one(EmbedPane)
            await _wait_until(
                lambda: pane._is_detail_view() and "问题行-0" in pane.render().plain,
                tries=300,
                interval=0.02,
            )
            self.assertEqual(pane.detail_offset, 0)
            # 直接调滚动 API 验证窗口机制（再测键盘绑定）
            self.assertTrue(pane.scroll_detail_page(1))
            self.assertGreater(pane.detail_offset, 0)
            top_after_page = pane.detail_offset
            await pilot.press("end")
            await pilot.pause()
            self.assertGreaterEqual(pane.detail_offset, top_after_page)
            self.assertTrue(pane.scroll_detail_home())
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


class KillKeepaliveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_key_confirm_kills_and_clears_keepalive_name(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": True, "pid": 4242, "keepalive_name": "pickup-claude-fake",
        }]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        with mock.patch("keepalive.kill") as kill_mock:
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


if __name__ == "__main__":
    unittest.main()
