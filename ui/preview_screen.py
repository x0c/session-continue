"""会话预览：全屏聊天记录，取代旧版 curses 全屏绘制 + 手写滚动/鼠标解析。

滚动/翻页交给 Textual 的 VerticalScroll 原生处理（PageUp/PageDown/Home/End/
鼠标滚轮全部内置），不再需要旧版手写的 SGR 鼠标解析和滚动位置数学。
"""

from __future__ import annotations

from rich.text import Text
from textual import work
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
        self._render_messages()
        self.set_interval(POLL_INTERVAL, self._poll_updates)

    def _render_messages(self) -> None:
        import pickup

        messages = self.store.get_conversation(self.session)
        runtime_name = self.store.registry.get(str(self.session.get("source") or "")).display_name
        scroll = self.query_one("#preview-scroll", VerticalScroll)
        width = max(20, scroll.content_size.width - 2)
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
        was_at_bottom = self._at_bottom
        self._at_bottom = scroll.scroll_y >= scroll.max_scroll_y - 1
        if was_at_bottom:
            scroll.scroll_end(animate=False)

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
