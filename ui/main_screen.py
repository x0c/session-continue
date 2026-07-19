"""主屏：会话列表 + 右栏内嵌面板，取代旧版 pickup.py 里的 _run 主循环。

按键语义（f 项目筛选 / p 固定 / space 预览 / a 高级操作 / n 新建 / e 全屏 /
x 关闭后台 / c 关闭面板 / Esc 退出）与旧版一一对应，具体业务规则不变；
只是从"手写 curses 循环 + 状态机"换成 Textual 的 action/binding 派发。
"""

from __future__ import annotations

import os
import time

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from ui.embed_pane import EmbedPane
from ui.modals import ConfirmModal, choose_target_runtime, new_session_flow
from ui.nav import NavState
from ui.preview_screen import PreviewScreen
from ui.session_list import SessionListView

try:
    from textual.screen import Screen
except ImportError:  # pragma: no cover
    from textual import Screen

REFRESH_INTERVAL = 3.0  # 秒，后台重扫会话列表的间隔，与旧版 _background_refresh 一致
LIST_PANE_WIDTH = 44  # 分栏时左栏固定宽度，对应旧版 EMBED_LEFT_BAND


class MainScreen(Screen):
    BINDINGS = [
        Binding("f", "cycle_project", "项目"),
        Binding("p", "pin_pane", "固定"),
        Binding("space", "preview", "预览"),
        Binding("a", "handoff", "高级操作"),
        Binding("n", "new_session", "新建"),
        Binding("e", "fullscreen", "全屏"),
        Binding("x", "kill_keepalive", "关闭后台", show=False),
        Binding("c", "close_pane", "关闭面板", show=False),
        Binding("m", "toggle_mouse", "鼠标", show=False),
        Binding("escape", "quit_app", "退出"),
        # 不再单独绑 ctrl+c 退出：Textual 的 Screen 基类自带 ctrl+c -> copy_text
        # （划词后复制选中文本），子类 BINDINGS 里重复同一个键会按键位覆盖掉
        # 基类那条，绑了就会让"划词选中 EmbedPane 里的文字后按 Ctrl+C 复制"失效。
        # 已用 Pilot 验证过：去掉这条后 ctrl+c 在运行时正确解析到
        # screen.copy_text。Esc 已是文档化的主退出键；未选中任何文本时按 Ctrl+C
        # 会走 Textual 默认的 help/quit 提示而非直接退出，但不影响 Esc 正常退出。
    ]

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        self.store = store
        self.embed_ok = embed_ok
        self.direct = direct
        self.osc_report = osc_report
        runtime_ids = store.registry.ids
        source = next((rid for rid in runtime_ids if store.sessions[rid]), runtime_ids[0])
        self.nav = NavState(source=source)
        self.mouse_forward_enabled = True

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="list-pane"):
                yield Static(id="list-header")
                yield SessionListView(self.store, self.nav, id="session-list")
            if self.embed_ok:
                yield EmbedPane(id="embed-pane", on_focus_list=self._focus_list, osc_report=self.osc_report)
        yield Footer()

    def on_mount(self) -> None:
        if self.embed_ok:
            self.query_one("#list-pane").styles.width = LIST_PANE_WIDTH
        self._update_header()
        import threading
        threading.Thread(target=self._background_refresh_loop, daemon=True).start()
        self.set_interval(0.5, self._poll_store)
        if self.direct is not None:
            # 直启子命令：焦点最终要落在内嵌面板上。SessionListView.focus() 走
            # Textual 的 call_later 延迟生效，如果这里也调用，会在 _host_direct_launch
            # 已经同步聚焦面板之后又把焦点抢回列表（真实 bug，靠真机 tmux 冒烟发现）。
            self._host_direct_launch()
        else:
            self.query_one(SessionListView).focus()
            self.call_after_refresh(self._follow_current_selection)

    # ---- 后台线程：磁盘重扫（与旧版同一套 threading 模型，只是唤醒目标从
    # curses 重绘换成 Textual 的列表重建） ----

    def _background_refresh_loop(self) -> None:
        while True:
            time.sleep(REFRESH_INTERVAL)
            try:
                if self.store.refresh():
                    self.store.dirty.set()
            except OSError:
                pass

    def _poll_store(self) -> None:
        self.store.poll_cache_updates()
        if self.store.dirty.is_set():
            self.store.dirty.clear()
            self.call_next(self._rebuild_list)

    async def _rebuild_list(self) -> None:
        await self.query_one(SessionListView).rebuild()
        self._update_header()

    def _update_header(self) -> None:
        session_list = self.query_one(SessionListView)
        filter_label = "全部项目" if self.nav.project_key is None else (
            os.path.basename(self.nav.project_key) or "未知项目"
        )
        count = len(session_list.visible_sessions())
        self.query_one("#list-header", Static).update(f" 会话 · {filter_label} ({count})")

    # ---- 选择跟随：右栏默认展示左栏当前选中项 ----

    def on_list_view_highlighted(self, event) -> None:
        self._follow_current_selection()

    def _follow_current_selection(self) -> None:
        if not self.embed_ok:
            return
        session_list = self.query_one(SessionListView)
        pane = self.query_one(EmbedPane)
        # 面板已持有键盘焦点时不跟随列表：直启会话、或用户已明确 Enter 聚焦某个
        # 托管会话，都不应被列表初次挂载/后台重扫触发的 highlight 事件覆盖画面。
        if pane.has_focus:
            return
        if self.nav.pinned_key is not None:
            return
        if session_list.is_new_session_selected():
            pane.show_detail(lambda: Text("新建会话：选择项目与运行时", justify="center"))
            return
        session = session_list.selected_session()
        if session is None:
            return
        name = session.get("keepalive_name")
        if name:
            pane.focus_session(str(name), lambda s=session: self._render_detail(s))
        else:
            pane.show_detail(lambda s=session: self._render_detail(s))

    def _render_detail(self, session: dict) -> Text:
        title = self.store.get_title(session)
        runtime = self.store.registry.get(str(session.get("source") or ""))
        status = "运行中" if session.get("live") else "已结束"
        project = str(session.get("cwd") or session.get("cwd_display") or "未知项目")
        out = Text(title, style="bold")
        out.append("\n" + f"{runtime.display_name} · {status}", style="dim")
        out.append("\n" + project, style="dim")
        out.append("\n\n最近提问\n", style="bold")
        out.append(str(session.get("last_user_msg") or "暂无可展示内容"))
        out.append("\n\n最近回复\n", style="bold")
        out.append(str(session.get("last_agent_msg") or "暂无可展示内容"))
        return out

    # ---- 会话选择/新建 ----

    @work
    async def on_list_view_selected(self, event) -> None:
        session_list = self.query_one(SessionListView)
        if session_list.is_new_session_selected():
            await self._start_new_session_flow()
            return
        session = session_list.selected_session()
        if session is None:
            return
        import pickup
        request = pickup.LaunchRequest(
            session, str(session.get("source") or self.nav.source), self.store.get_title(session)
        )
        await self._open_or_exit(request)

    async def _start_new_session_flow(self) -> None:
        session_list = self.query_one(SessionListView)
        sessions = session_list.visible_sessions()
        anchor_session = sessions[0] if sessions else None
        request = await new_session_flow(self.app, self.store, self.nav, anchor_session)
        if request is not None:
            await self._open_or_exit(request)

    async def _open_or_exit(self, request) -> None:
        """embed 可用则原地内嵌打开；否则退出应用，交给外层 execvp 全屏接管。"""
        if self.embed_ok:
            self._embed_open(request)
        else:
            self.app.exit(result=request)

    def _embed_open(self, request) -> None:
        import embed
        import keepalive
        import pickup

        same_runtime = isinstance(request, pickup.LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
        )
        pane = self.query_one(EmbedPane)
        if isinstance(request, pickup.LaunchRequest):
            existing = request.session.get("keepalive_name") if same_runtime else None
            if existing:
                pane.focus_session(
                    str(existing), lambda s=request.session: self._render_detail(s),
                )
                self.set_focus(pane)
                return
            plan = self.store.registry.build_launch_plan(request)
            ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
        else:
            plan = self.store.registry.build_new_session_plan(request)
            ident = keepalive.new_session_ident()

        pane_size = pane.content_size
        try:
            name = embed.host_session(
                plan, request.target_runtime_id, ident,
                max(20, pane_size.width), max(4, pane_size.height),
                osc_report=self.osc_report,
            )
        except (embed.EmbedError, pickup.LaunchError):
            self.app.bell()
            return
        if same_runtime:
            request.session["keepalive_name"] = name
            self.store.hosted[pickup.session_key(request.session)] = name
        fallback = None
        if isinstance(request, pickup.LaunchRequest):
            fallback = lambda s=request.session: self._render_detail(s)
        pane.focus_session(name, fallback)
        self.set_focus(pane)
        self.call_next(self._rebuild_list)

    def _host_direct_launch(self) -> None:
        import embed

        direct = self.direct
        pane = self.query_one(EmbedPane)
        pane_size = pane.content_size
        try:
            name = embed.host_session(
                direct.plan, direct.runtime_id, direct.ident,
                max(20, pane_size.width), max(4, pane_size.height),
                osc_report=self.osc_report,
            )
        except embed.EmbedError:
            self.app.bell()
            return
        pane.focus_session(name)
        self.set_focus(pane)

    def _focus_list(self) -> None:
        self.query_one(SessionListView).focus()

    # ---- 动作 ----

    async def action_cycle_project(self) -> None:
        await self.query_one(SessionListView).cycle_project_filter()
        self._update_header()
        self._follow_current_selection()

    def action_pin_pane(self) -> None:
        import pickup

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        key = pickup.session_key(session)
        if self.nav.pinned_key == key:
            self.nav.pinned_key = None
            self._follow_current_selection()
        else:
            self.nav.pinned_key = key

    @work
    async def action_preview(self) -> None:
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        title = self.store.get_title(session)
        result = await self.app.push_screen_wait(PreviewScreen(self.store, self.nav, session, title))
        if result is None:
            return
        request, force_fullscreen = result
        if not force_fullscreen and self.embed_ok:
            self._embed_open(request)
        else:
            self.app.exit(result=request)

    @work
    async def action_handoff(self) -> None:
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        target = await choose_target_runtime(
            self.app, self.store, str(session.get("source") or self.nav.source)
        )
        if target is None:
            return
        import pickup
        request = pickup.LaunchRequest(session, target, self.store.get_title(session))
        await self._open_or_exit(request)

    async def action_new_session(self) -> None:
        import pickup

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        cwd = pickup.usable_cwd(pickup._new_session_cwd(self.store, self.nav, session))
        if cwd is None:
            self.app.bell()
            return
        request = pickup.NewSessionRequest(self.nav.source, cwd)
        await self._open_or_exit(request)

    @work
    async def action_kill_keepalive(self) -> None:
        import keepalive
        import pickup

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        keepalive_name = session.get("keepalive_name") if session else None
        if not keepalive_name:
            self.app.bell()
            return
        title = self.store.get_title(session)
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(f"关闭后台进程「{title}」？未保存的当前任务进度将丢失")
        )
        if not confirmed:
            return
        keepalive.kill(keepalive_name)
        session.pop("keepalive_name", None)
        self.store.hosted.pop(pickup.session_key(session), None)
        await self._rebuild_list()

    def action_close_pane(self) -> None:
        if not self.embed_ok:
            return
        self.query_one(EmbedPane).clear()
        self._focus_list()

    def action_toggle_mouse(self) -> None:
        self.mouse_forward_enabled = not self.mouse_forward_enabled

    def action_fullscreen(self) -> None:
        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        if session is None:
            self.app.bell()
            return
        import pickup
        request = pickup.LaunchRequest(session, self.nav.source, self.store.get_title(session))
        self.app.exit(result=request)

    def action_quit_app(self) -> None:
        self.app.exit(result=None)
