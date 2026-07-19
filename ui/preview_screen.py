"""会话预览：全屏聊天记录，取代旧版 curses 全屏绘制 + 手写滚动/鼠标解析。

滚动/翻页交给 Textual 的 VerticalScroll 原生处理（PageUp/PageDown/Home/End/
鼠标滚轮全部内置），不再需要旧版手写的 SGR 鼠标解析和滚动位置数学。
"""

from __future__ import annotations

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ui.modals import choose_target_runtime, new_session_flow

POLL_INTERVAL = 1.0  # 与旧版「约 1 秒检查一次历史文件新写入」保持一致的节奏


class PreviewScreen(Screen):
    """返回 (LaunchRequest, 是否强制全屏) 或 None（仅关闭预览）。"""

    BINDINGS = [("escape", "close", "关闭"), ("space", "close", "关闭")]

    DEFAULT_CSS = """
    PreviewScreen {
        layers: base;
    }
    PreviewScreen > VerticalScroll {
        height: 1fr;
    }
    #preview-body {
        width: 100%;
        padding: 0 1;
    }
    """

    def __init__(self, store, nav, session: dict, title: str) -> None:
        super().__init__()
        self.store = store
        self.nav = nav
        self.session = session
        self.title = title
        self._at_bottom = True
        # 上一次真正重渲染时用到的消息列表/折行宽度：轮询命中"内容没变、宽度
        # 也没变"时直接跳过折行+重建 Text+Static.update()（全量 re-layout），
        # 只有内容变化或窗口 resize 才值得付这个代价。
        self._last_messages: list | None = None
        self._last_width: int | None = None

    def compose(self) -> ComposeResult:
        import pickup

        session_id = str(self.session.get("id") or "")
        header = f" 对话预览 · {self.title} "
        if session_id:
            header += f"  Session ID {session_id}"
        yield Static(header, id="preview-header")
        with VerticalScroll(id="preview-scroll"):
            yield Static(id="preview-body")
        yield Static(
            "↑↓/PgUp/PgDn/Home/End 滚动  Enter 恢复  e 全屏  a 接力  n 新建  Space/Esc 关闭",
            id="preview-footer",
        )

    def on_mount(self) -> None:
        self._render_messages(force=True)
        self.set_interval(POLL_INTERVAL, self._poll_updates)

    def _on_resize(self, event: events.Resize) -> None:
        # 无条件轮询改成"内容没变就跳过"之后，窗口 resize 不再顺带触发重渲染
        # 了——但折行宽度是按当前终端宽度算的（见下方 width 计算），宽度变了
        # 必须强制重新折行，否则文本会保持旧宽度的断行，直到内容碰巧也变化。
        self._render_messages(force=True)

    def _render_messages(self, *, force: bool = False) -> None:
        import pickup

        messages = self.store.get_conversation(self.session)
        scroll = self.query_one("#preview-scroll", VerticalScroll)
        width = max(20, scroll.content_size.width - 2)
        # 每次轮询都以 Textual 当前真实滚动位置更新追底状态，必须放在内容相同的
        # early return 之前。用户刚向上翻历史时，即使这一轮没有新消息，也要立刻
        # 记住“已离开底部”，避免下一轮内容更新把他强行拽回最底端。
        was_at_bottom = scroll.scroll_y >= scroll.max_scroll_y - 1
        self._at_bottom = was_at_bottom
        if not force and messages == self._last_messages and width == self._last_width:
            return

        runtime_name = self.store.registry.get(str(self.session.get("source") or "")).display_name
        lines = pickup._preview_lines(messages, runtime_name, width)

        out = Text()
        for i, (kind, line, suffix) in enumerate(lines):
            style = {"user": "bold cyan", "assistant": "bold green", "dim": "dim"}.get(kind, "")
            out.append(line, style=style)
            if suffix:
                out.append(suffix, style="dim")
            if i != len(lines) - 1:
                out.append("\n")
        body = self.query_one("#preview-body", Static)
        body.update(out)
        self._last_messages = messages
        self._last_width = width
        if was_at_bottom:
            scroll.scroll_end(animate=False)
            self._at_bottom = True

    def _poll_updates(self) -> None:
        self._render_messages()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        import pickup

        if event.key == "enter":
            event.stop()
            request = pickup.LaunchRequest(
                self.session, str(self.session.get("source") or self.nav.source), self.title
            )
            self.dismiss((request, False))
        elif event.key == "e":
            event.stop()
            request = pickup.LaunchRequest(
                self.session, str(self.session.get("source") or self.nav.source), self.title
            )
            self.dismiss((request, True))
        elif event.key == "a":
            event.stop()
            self._do_handoff()
        elif event.key == "n":
            event.stop()
            self._do_new_session()

    @work
    async def _do_handoff(self) -> None:
        import pickup

        target = await choose_target_runtime(
            self.app, self.store, str(self.session.get("source") or self.nav.source)
        )
        if target is not None:
            request = pickup.LaunchRequest(self.session, target, self.title)
            self.dismiss((request, False))

    @work
    async def _do_new_session(self) -> None:
        request = await new_session_flow(self.app, self.store, self.nav, self.session)
        if request is not None:
            self.dismiss((request, False))
