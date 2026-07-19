"""主屏：左栏会话列表 + 右栏预览/内嵌终端（pickup 唯一界面）。

按键语义（/ 聚焦项目搜索 / a 高级操作 / n 新建 /
q 结束会话 / c 关闭面板 / Esc 退出）；选中非进行中会话时右栏直接
展示完整对话预览。侧边栏选中或回车托管后，键盘焦点仍留在列表——只有鼠标点到右栏
才与内嵌会话交互；右栏滚轮/预览翻页与焦点无关，鼠标在右栏上即可滚动。
侧边栏顶部为项目搜索框，大小写无关模糊匹配项目名与会话标题。
禁止再加第二套全屏预览或纯列表旧界面。
"""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input
from textual.worker import get_current_worker

import dataclasses

from pickup import i18n
from pickup.i18n import t
from pickup.ui.embed_pane import EmbedPane
from pickup.ui.modals import ConfirmModal, choose_target_runtime, new_session_flow
from pickup.ui.nav import NavState
from pickup.ui.session_list import SessionListView

try:
    from textual.screen import Screen
except ImportError:  # pragma: no cover
    from textual import Screen

REFRESH_INTERVAL = 3.0  # 秒，后台重扫会话列表的最短间隔，与旧版 _background_refresh 一致
REFRESH_INTERVAL_MAX = 10.0  # 秒，连续空闲多轮后退避到的最长间隔
_IDLE_ROUNDS_BEFORE_BACKOFF = 3  # 连续几轮扫描都没变化才开始拉长间隔，避免偶发抖动误判空闲
CACHE_POLL_INTERVAL = 0.5  # 秒，标题缓存文件轮询间隔（比会话重扫轻得多，保持高频）
LIST_PANE_WIDTH = 39  # 分栏时左栏固定宽度，对应旧版 EMBED_LEFT_BAND

# 动作名 → 文案 key；实例化时只改 description，不能整表替换（会丢掉 ListView/Screen 继承绑键）
_ACTION_I18N = {
    "handoff": "action.advanced",
    "new_session": "action.new",
    "kill_keepalive": "action.kill_session",
    "close_pane": "action.close_pane",
    "save_screenshot": "action.screenshot",
    "preview_home": "action.preview_home",
    "preview_end": "action.preview_end",
    "preview_page_up": "action.preview_page_up",
    "preview_page_down": "action.preview_page_down",
    "quit_app": "action.quit",
}


def _main_bindings() -> list[Binding]:
    """按当前语言生成底部快捷键说明。"""
    return [
        Binding("a", "handoff", t("action.advanced")),
        Binding("n", "new_session", t("action.new")),
        Binding("q", "kill_keepalive", t("action.kill_session")),
        Binding("c", "close_pane", t("action.close_pane"), show=False),
        Binding("f12", "save_screenshot", t("action.screenshot"), show=False),
        # 右栏静态对话预览滚动（列表聚焦时也生效；优先级高于 ListView 的同名键）
        Binding("home", "preview_home", t("action.preview_home"), show=False, priority=True),
        Binding("end", "preview_end", t("action.preview_end"), show=False, priority=True),
        Binding("pageup", "preview_page_up", t("action.preview_page_up"), show=False, priority=True),
        Binding("pagedown", "preview_page_down", t("action.preview_page_down"), show=False, priority=True),
        Binding("escape", "quit_app", t("action.quit")),
        # 不再单独绑 ctrl+c 退出：Textual 的 Screen 基类自带 ctrl+c -> copy_text
        # （划词后复制选中文本），子类 BINDINGS 里重复同一个键会按键位覆盖掉
        # 基类那条，绑了就会让"划词选中 EmbedPane 里的文字后按 Ctrl+C 复制"失效。
        # 已用 Pilot 验证过：去掉这条后 ctrl+c 在运行时正确解析到
        # screen.copy_text。Esc 已是文档化的主退出键；未选中任何文本时按 Ctrl+C
        # 会走 Textual 默认的 help/quit 提示而非直接退出，但不影响 Esc 正常退出。
    ]


def _localize_binding_descriptions(node) -> None:
    """就地刷新已合并绑键的 description，保留继承来的 up/down/enter 等。"""
    for key, bindings in list(node._bindings.key_to_bindings.items()):
        node._bindings.key_to_bindings[key] = [
            dataclasses.replace(b, description=t(_ACTION_I18N[b.action]))
            if b.action in _ACTION_I18N
            else b
            for b in bindings
        ]


class MainScreen(Screen):
    BINDINGS = _main_bindings()

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        _localize_binding_descriptions(self)
        self.store = store
        self.embed_ok = embed_ok
        self.direct = direct
        self.osc_report = osc_report
        runtime_ids = store.registry.ids
        source = next((rid for rid in runtime_ids if store.sessions[rid]), runtime_ids[0])
        self.nav = NavState(source=source)
        self._host_busy = False
        self._preview_gen = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="list-pane"):
                yield Input(placeholder=t("filter.placeholder"), id="project-search")
                yield SessionListView(self.store, self.nav, id="session-list")
            if self.embed_ok:
                yield EmbedPane(id="embed-pane", on_focus_list=self._focus_list, osc_report=self.osc_report)
        yield Footer()

    def on_mount(self) -> None:
        if self.embed_ok:
            self.query_one("#list-pane").styles.width = LIST_PANE_WIDTH
        self._update_header()
        # store 可能已经 load() 过（如 _dispatch_direct_launch、测试里的 _make_store
        # 都是同步预加载好再传进来），也可能还没有（main() 现在把 load() 挪到后台
        # 线程异步跑，UI 先渲染骨架）。已加载时直接进入后台重扫循环；未加载时先挂
        # 一个 worker 等它跑完，完成后再启动后台重扫，避免两者并发调用同一个
        # registry.scan_all()（RuntimeRegistry 的廉价预检缓存不是线程安全的）。
        if self.store.loaded:
            self._start_background_refresh()
        else:
            self._await_initial_load()
        self.set_interval(CACHE_POLL_INTERVAL, self._poll_cache)
        if self.direct is not None:
            # 直启子命令：焦点最终要落在内嵌面板上（用户就是来操作新会话的）。
            # 不要先调 SessionListView.focus()——它走 call_later，会在托管完成后
            # 把焦点抢回列表（真机冒烟回归过）。
            self._host_direct_launch()
        else:
            self.query_one(SessionListView).focus()
            self.call_after_refresh(self._follow_current_selection)

    # ---- 首屏异步加载：main() 把 store.load() 挪到后台线程异步跑，这里等它跑完
    # 再渲染真实列表（骨架已经在 compose() 时就显示出来了：空列表 + "＋ 新建会话"） ----

    @work(thread=True, group="initial-load")
    def _await_initial_load(self) -> None:
        worker = get_current_worker()
        # 短超时轮询让 Screen 卸载/测试退出时能及时响应 worker.cancel()，不能永久
        # 卡在一次无期限 Event.wait() 里拖住 run_test 或真实应用退出。
        while not worker.is_cancelled:
            if self.store.wait_loaded(timeout=0.1):
                if not worker.is_cancelled:
                    self.app.call_from_thread(self._on_initial_load_done)
                return

    def _on_initial_load_done(self) -> None:
        # __init__ 时 store 还没扫完，默认来源只能先假定成 registry 里的第一个；
        # 扫完后如果它其实没有会话而别的运行时有，重新选一次，跟 __init__ 里
        # "挑第一个有会话的运行时"这条默认选择逻辑保持一致。
        if not self.store.sessions.get(self.nav.source):
            alt = next(
                (rid for rid in self.store.registry.ids if self.store.sessions[rid]), None,
            )
            if alt is not None:
                self.nav.source = alt
        self.call_next(self._rebuild_and_follow)
        self._start_background_refresh()

    async def _rebuild_and_follow(self) -> None:
        await self._rebuild_list()

    # ---- 后台重扫：Textual worker（取代旧版裸 threading.Thread + 0.5s dirty 轮询），
    # 发现变化直接 call_from_thread 触发重建，不再有轮询延迟；连续空闲多轮后自适应
    # 拉长扫描间隔，省磁盘/CPU；任何异常都要捕获且继续循环，不能让后台线程静默死掉 ----

    def _start_background_refresh(self) -> None:
        self._background_refresh_worker()

    @work(thread=True, exclusive=True, group="session-refresh")
    def _background_refresh_worker(self) -> None:
        import pickup

        worker = get_current_worker()
        interval = REFRESH_INTERVAL
        idle_rounds = 0
        while not worker.is_cancelled:
            # cancelled_event.wait() 同时承担定时器和取消唤醒；Screen 一退出便立即
            # 返回，不再被 time.sleep(10) 拖住。
            if worker.cancelled_event.wait(interval):
                return
            had_error = self.store.get_load_error() is not None
            try:
                changed = self.store.refresh()
            except Exception as exc:  # 全异常兜底：只捕获 OSError 曾经让这个线程
                # 遇到未预料异常（如扫描器 bug）就静默死掉，此后列表再也不会更新
                # 且没有任何提示；模式与 ui/embed_pane.py 的 _capture_loop 一致，
                # 复用同一个错误日志，写文件留证并继续循环，而不是让线程退出。
                pickup._log_embed_error("后台会话重扫线程", exc)
                idle_rounds = 0
                interval = REFRESH_INTERVAL
                if not worker.is_cancelled:
                    self.app.call_from_thread(self._update_header)
                continue
            if worker.is_cancelled:
                return
            recovered = had_error and self.store.get_load_error() is None
            if changed:
                idle_rounds = 0
                interval = REFRESH_INTERVAL
                self.app.call_from_thread(self._rebuild_list)
            else:
                if recovered:
                    self.app.call_from_thread(self._update_header)
                idle_rounds += 1
                if idle_rounds >= _IDLE_ROUNDS_BEFORE_BACKOFF:
                    interval = min(REFRESH_INTERVAL_MAX, interval * 2)

    def _poll_cache(self) -> None:
        """标题缓存文件轮询：比会话重扫轻得多（只 stat 一个文件），保持独立的
        高频轮询；命中变化时复用同一个 store.dirty 事件当"待重建"标志。"""
        self.store.poll_cache_updates()
        if self.store.dirty.is_set():
            self.store.dirty.clear()
            self.call_next(self._rebuild_list)

    async def _rebuild_list(self) -> None:
        await self.query_one(SessionListView).rebuild()
        self._update_header()
        if self.embed_ok:
            pane = self.query_one(EmbedPane)
            # SessionCard 已换成最新扫描对象；静态详情缓存也必须失效并重新跟随，
            # 否则右栏仍会展示旧闭包里的标题/状态/对话预览。
            pane.invalidate_detail()
            self._follow_current_selection()

    def _update_header(self) -> None:
        """刷新搜索框占位文案：空查询时展示命中数；出错/无会话时给出原因。"""
        session_list = self.query_one(SessionListView)
        search = self.query_one("#project-search", Input)
        count = len(session_list.visible_sessions())
        load_error = self.store.get_load_error()
        # 首屏扫描已经跑完（store.loaded）且全部运行时都没扫到任何会话时，给出
        # 友好提示，而不是让用户面对一个永远空白、原因不明的列表——旧版是在 main()
        # 里同步扫完就直接打印错误退出，扫描挪到后台 worker 后这个判断只能挪到这里，
        # 扫描没跑完之前（store.loaded 为 False）不能误判为"确实没有会话"。
        if load_error:
            search.placeholder = t("filter.load_error", error=load_error)
        elif self.store.loaded and count == 0 and not any(self.store.sessions.values()):
            names = i18n.join_names(
                [runtime.display_name for runtime in self.store.registry]
            )
            search.placeholder = t("filter.no_sessions", names=names)
        elif self.nav.project_query.strip():
            search.placeholder = t("filter.placeholder_count_active", count=count)
        else:
            search.placeholder = t("filter.placeholder_count", count=count)

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
        if session_list.is_new_session_selected():
            pane.show_detail(lambda: Text(t("detail.new_session_hint")))
            return
        session = session_list.selected_session()
        if session is None:
            return
        name = session.get("keepalive_name")
        if name:
            pane.focus_session(str(name), lambda s=session: self._render_detail(s))
        else:
            self._preview_gen += 1
            self._warm_conversation(session, self._preview_gen)
            pane.show_detail(lambda s=session: self._render_detail(s))

    def _detail_header(self, session: dict) -> Text:
        import pickup

        title = self.store.get_title(session)
        runtime = self.store.registry.get(str(session.get("source") or ""))
        status = t("status.running") if session.get("live") else t("status.ended")
        project = str(
            session.get("cwd") or session.get("cwd_display") or t("project.unknown")
        )
        out = Text(title, style="bold")
        out.append("\n")
        out.append(runtime.display_name, style=pickup.runtime_label_style(runtime.id))
        out.append(f" · {status}", style="dim")
        out.append("\n" + project, style="dim")
        return out

    def _render_detail(self, session: dict) -> Text:
        import pickup

        # 详情 renderer 会被 EmbedPane 缓存并延后调用；后台重扫后闭包捕获的 dict
        # 已不是 Store 当前对象，必须每次按稳定会话键重新解析最新快照。
        session = self.store.find_session(pickup.session_key(session)) or session
        out = self._detail_header(session)
        messages = self.store.peek_conversation(session)
        if messages is None:
            return out
        runtime = self.store.registry.get(str(session.get("source") or ""))
        runtime_name = runtime.display_name
        runtime_style = pickup.runtime_label_style(runtime.id)
        try:
            width = max(20, (self.query_one(EmbedPane).size.width or 40) - 2)
        except Exception:
            width = 40
        lines = pickup._preview_lines(messages, runtime_name, width)
        out.append("\n")
        for i, (kind, line, suffix) in enumerate(lines):
            out.append("\n")
            if kind == "assistant" and line.startswith("◆ "):
                style = runtime_style
            else:
                style = {"user": "bold cyan", "assistant": "bold green", "dim": "dim"}.get(kind, "")
            out.append(line, style=style)
            if suffix:
                out.append(suffix, style="dim")
        return out

    @work(thread=True)
    def _warm_conversation(self, session: dict, gen: int) -> None:
        """后台填对话缓存；仅当仍是当前选中世代时刷新右栏。"""
        try:
            self.store.get_conversation(session)
        except Exception:
            return
        if gen != self._preview_gen:
            return
        self.app.call_from_thread(self._refresh_preview_detail)

    def _refresh_preview_detail(self) -> None:
        if not self.embed_ok:
            return
        pane = self.query_one(EmbedPane)
        if pane.session_name or pane.has_focus:
            return
        pane.invalidate_detail()

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
        """准备启动计划（不涉及阻塞 I/O）后，把 `embed.host_session` 这个真正阻塞的
        tmux 子进程调用甩给后台 worker（见 `_host_and_focus`），不在 Textual 事件
        循环所在线程上跑——tmux 卡顿（系统负载高/磁盘慢）时 `_CREATE_TIMEOUT` 上限
        有 5s，同步跑会把整个 UI 冻住那么久。"""
        from pickup import keepalive
        import pickup

        same_runtime = isinstance(request, pickup.LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
        )
        pane = self.query_one(EmbedPane)
        if isinstance(request, pickup.LaunchRequest):
            key = pickup.session_key(request.session)
            current = self.store.find_session(key) or request.session
            request = pickup.LaunchRequest(current, request.target_runtime_id, request.title)
            existing = request.session.get("keepalive_name") if same_runtime else None
            if existing:
                # 只挂接右栏画面，不抢键盘焦点——点右栏才与内嵌会话交互。
                pane.focus_session(
                    str(existing), lambda s=request.session: self._render_detail(s),
                )
                return
            if self._host_busy:
                self.app.bell()
                return
            plan = self.store.registry.build_launch_plan(request)
            ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
        else:
            if self._host_busy:
                self.app.bell()
                return
            plan = self.store.registry.build_new_session_plan(request)
            ident = keepalive.new_session_ident()

        # pane.content_size 是 Textual 的 DOM/布局状态，必须在主线程读完再进 worker，
        # 不能在后台线程里访问 Widget 属性。
        pane_size = pane.content_size
        self._host_busy = True
        self._host_and_focus(
            request, plan, ident, same_runtime, max(20, pane_size.width), max(4, pane_size.height),
        )

    @work(thread=True, group="host")
    def _host_and_focus(self, request, plan, ident, same_runtime, width, height) -> None:
        from pickup import embed
        from pickup import observe
        import pickup
        import time

        t0 = time.perf_counter()
        runtime = request.target_runtime_id
        try:
            name = embed.host_session(
                plan, request.target_runtime_id, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("内嵌会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(self._on_embed_hosted, request, name, same_runtime)

    def _on_host_failed(self) -> None:
        """host worker 失败收尾：释放单飞锁并给用户终端响铃。"""
        self._host_busy = False
        self.app.bell()

    def _on_embed_hosted(self, request, name: str, same_runtime: bool) -> None:
        """`_host_and_focus` worker 成功后的收尾：只在主线程操作 Textual/store 状态。

        `request` 可能是 `LaunchRequest`（恢复/接力）或 `NewSessionRequest`（空白新建）。
        后者没有关联会话，不能读 `.session`——按 `n` 新建时曾经因此闪退。
        """
        import pickup

        self._host_busy = False
        pane = self.query_one(EmbedPane)
        fallback = None
        if isinstance(request, pickup.LaunchRequest):
            current = request.session
            if same_runtime:
                key = pickup.session_key(request.session)
                marked = self.store.mark_hosted(key, name)
                if marked is None:
                    request.session["keepalive_name"] = name
                current = marked or request.session
            fallback = lambda s=current: self._render_detail(s)
        # 托管成功只更新右栏画面；键盘焦点留在侧边栏，点右栏才进入内嵌交互。
        pane.focus_session(name, fallback)
        self.call_next(self._rebuild_list)

    def _host_direct_launch(self) -> None:
        if self._host_busy:
            self.app.bell()
            return
        direct = self.direct
        pane = self.query_one(EmbedPane)
        pane_size = pane.content_size
        self._host_busy = True
        self._host_direct_worker(direct, max(20, pane_size.width), max(4, pane_size.height))

    @work(thread=True, group="host")
    def _host_direct_worker(self, direct, width: int, height: int) -> None:
        from pickup import embed
        from pickup import observe
        import pickup
        import time

        t0 = time.perf_counter()
        runtime = direct.runtime_id
        try:
            name = embed.host_session(
                direct.plan, direct.runtime_id, direct.ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            pickup._log_embed_error("直启会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(self._on_direct_hosted, name)

    def _on_direct_hosted(self, name: str) -> None:
        self._host_busy = False
        pane = self.query_one(EmbedPane)
        pane.focus_session(name)
        # 直启是「打开就操作」：焦点落到右栏。侧边栏点选/回车路径不抢焦点。
        self.set_focus(pane)

    def _focus_list(self) -> None:
        self.query_one(SessionListView).focus()

    # ---- 动作 ----

    def action_focus_search(self) -> None:
        self.query_one("#project-search", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "project-search":
            return
        self.nav.project_query = event.value
        await self.query_one(SessionListView).rebuild(keep_selection=True)
        self._update_header()
        self._follow_current_selection()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "project-search":
            return
        # Enter：把焦点交回列表，方便继续用 j/k / 回车操作会话
        list_view = self.query_one(SessionListView)
        list_view.focus()
        if list_view.index is None:
            list_view.index = 1 if list_view.visible_sessions() else 0

    def on_key(self, event) -> None:
        search = self.query_one("#project-search", Input)
        list_view = self.query_one(SessionListView)
        if search.has_focus:
            # 搜索框内 Down：跳到列表；不在这里绑 /，避免吞掉用户想输入的斜杠
            if event.key == "down":
                event.stop()
                list_view.focus()
                if list_view.index is None:
                    list_view.index = 1 if list_view.visible_sessions() else 0
            return
        # 列表聚焦时 / 打开搜索（不用 Screen Binding，否则搜索框里按 / 会被截走）
        if event.key == "slash" and list_view.has_focus:
            event.stop()
            search.focus()

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
        from pickup import keepalive
        import pickup

        session_list = self.query_one(SessionListView)
        session = session_list.selected_session()
        keepalive_name = session.get("keepalive_name") if session else None
        if not keepalive_name:
            self.app.bell()
            return
        title = self.store.get_title(session)
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(t("confirm.kill_session", title=title))
        )
        if not confirmed:
            return
        keepalive.kill(keepalive_name)
        self.store.mark_hosted(pickup.session_key(session), None)
        await self._rebuild_list()

    def action_close_pane(self) -> None:
        if not self.embed_ok:
            return
        self.query_one(EmbedPane).clear()
        self._focus_list()

    def action_preview_home(self) -> None:
        if self.embed_ok:
            self.query_one(EmbedPane).scroll_detail_home()

    def action_preview_end(self) -> None:
        if self.embed_ok:
            self.query_one(EmbedPane).scroll_detail_end()

    def action_preview_page_up(self) -> None:
        if self.embed_ok:
            self.query_one(EmbedPane).scroll_detail_page(-1)

    def action_preview_page_down(self) -> None:
        if self.embed_ok:
            self.query_one(EmbedPane).scroll_detail_page(1)

    def action_save_screenshot(self) -> None:
        """F12：导出当前 TUI 到 ~/.cache/pickup/screenshots/（用户主动触发）。"""
        from pickup import observe

        try:
            path = observe.save_tui_screenshot(self.app)
        except Exception as exc:  # noqa: BLE001
            import pickup
            pickup._log_embed_error("TUI 截图", exc)
            self.app.bell()
            return
        self.notify(t("notify.screenshot", path=path), title="pickup", timeout=4)

    def action_quit_app(self) -> None:
        # 搜索框聚焦时 Esc 先清空查询，再交回列表；列表上 Esc 才真正退出
        search = self.query_one("#project-search", Input)
        if search.has_focus:
            if search.value:
                search.value = ""
                return
            self.query_one(SessionListView).focus()
            return
        self.app.exit(result=None)
