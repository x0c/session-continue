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

import shutil
import subprocess
import time
import unittest
from unittest import mock

import pickup
from models import LaunchPlan
from ui.app import PickupApp
from ui.embed_pane import EmbedPane
from ui.modals import ConfirmModal, PickMenuModal, RuntimePickerModal
from ui.preview_screen import PreviewScreen
from ui.session_list import SessionCard, SessionListView

HAS_TMUX = shutil.which("tmux") is not None


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


class MainScreenNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_selection_and_project_filter_cycle(self) -> None:
        """回归测试：project_key 曾经在 SessionListView 和 MainScreen.nav 上各存
        一份、互不同步——按 f 筛选后，实际过滤生效（session_list 内部那份变了）
        但页头文案（读 nav.project_key）继续显示"全部项目"，两者对不上（真机
        跑 selftest.sh 才暴露，headless 测试当时只断言了内部状态，没断言页头
        渲染文本，因此没拦住）。这里必须同时断言页头渲染文本，不能只看内部值。
        """
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            list_view = app.screen.query_one(SessionListView)
            header = app.screen.query_one("#list-header")
            self.assertEqual(list_view.index, 0)  # 固定「+新建会话」项
            self.assertEqual(len(list_view.visible_sessions()), 3)
            self.assertIn("全部项目", str(header.content))

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(list_view.index, 1)

            await pilot.press("f")
            await pilot.pause()
            self.assertEqual(list_view.nav.project_key, "/tmp")
            self.assertIn("tmp", str(header.content))
            self.assertNotIn("全部项目", str(header.content))

            await pilot.press("f")
            await pilot.pause()
            self.assertIsNone(list_view.nav.project_key)
            self.assertIn("全部项目", str(header.content))

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
            await pilot.pause(delay=0.5)
            pane = app.screen.query_one(EmbedPane)
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

            await pilot.mouse_down(pane, offset=(0, 0))
            await pilot.hover(pane, offset=(14, 0))
            await pilot.mouse_up(pane, offset=(14, 0))
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
        self.assertIn("GOT-SIGINT", pane.render().plain)

    async def test_host_session_failure_bells_and_stays_in_list(self) -> None:
        store, registry = _make_store()
        registry.build_launch_plan = lambda request: LaunchPlan(("claude",), None)

        app = PickupApp(store, embed_ok=True)
        with mock.patch("embed.host_session", side_effect=__import__("embed").EmbedError("boom")):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
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
            await pilot.pause(delay=0.5)
            pane = app.screen.query_one(EmbedPane)
            self.assertIsNotNone(pane.session_name)
            self._hosted_names.append(pane.session_name)
            await _wait_for_pane_text(pane, "DIRECT-HELLO")
            self.assertTrue(pane.has_focus)

            # 键盘输入必须真的到达托管会话，而不是被抢回焦点的列表吞掉
            await pilot.press(*"x")
            await _wait_for_pane_text(pane, "x")
            self.assertIn("x", pane.render().plain.split("DIRECT-HELLO")[-1])


class PreviewScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_messages_and_escape_closes(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            self.assertIsInstance(app.screen, PreviewScreen)
            body = str(app.screen.query_one("#preview-body").content)
            self.assertIn("测试问题", body)
            self.assertIn("测试回复", body)

            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PreviewScreen)

    async def test_enter_dismisses_with_launch_request(self) -> None:
        store, _ = _make_store()
        app = PickupApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)

    async def test_a_key_opens_handoff_modal_and_picks_target_runtime(self) -> None:
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
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            await pilot.press("down")  # claude(原生恢复) -> codex
            await pilot.press("enter")
            await pilot.pause()
        self.assertIsInstance(app.return_value, pickup.LaunchRequest)
        self.assertEqual(app.return_value.target_runtime_id, "codex")


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

    async def test_confirm_modal_y_confirms_other_key_cancels(self) -> None:
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


class KillKeepaliveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_x_key_confirm_kills_and_clears_keepalive_name(self) -> None:
        sessions = [{
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False, "keepalive_name": "pickup-claude-fake",
        }]
        store, _ = _make_store(sessions=sessions)
        app = PickupApp(store, embed_ok=False)
        with mock.patch("keepalive.kill") as kill_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.2)
                await pilot.press("down")
                await pilot.press("x")
                await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmModal)
                await pilot.press("y")
                await pilot.pause()
        kill_mock.assert_called_once_with("pickup-claude-fake")
        self.assertNotIn("keepalive_name", sessions[0])


if __name__ == "__main__":
    unittest.main()
