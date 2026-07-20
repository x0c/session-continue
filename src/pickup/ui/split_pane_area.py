"""右侧分屏区：助手顶栏 + 最多三格均分内嵌终端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

from pickup.i18n import t
from pickup.models import session_key as make_session_key
from pickup.split_layout import MAX_PANES
from pickup.ui.embed_pane import EmbedPane
from pickup.ui.runtime_top_bar import RuntimeTopBar


@dataclass
class PaneSpec:
    """单格绑定的会话。"""

    session_key: str
    keepalive_name: str | None = None
    cell_id: str = ""


class _PaneClose(Static):
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _PaneClose {
        width: 3;
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    _PaneClose:hover {
        color: $error;
        background: $error-darken-3;
    }
    """

    def __init__(self, on_close: Callable[[], None], **kwargs) -> None:
        super().__init__("✕", **kwargs)
        self._on_close = on_close

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_close()


class _PaneHeader(Horizontal):
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _PaneHeader {
        height: 1;
        width: 1fr;
        margin: 0;
        padding: 0;
        color: auto 90%;
        background: $surface;
    }
    _PaneHeader.-active {
        color: auto 90%;
        /* 活跃格用主色弱化底，避免整条高饱和蓝条抢过内嵌内容 */
        background: $primary-muted;
    }
    _PaneHeader.-active _PaneClose {
        color: auto 90%;
    }
    _PaneHeader Static.title {
        width: 1fr;
        height: 1;
        content-align: left middle;
        margin: 0;
        padding: 0;
        text-overflow: ellipsis;
    }
    """

    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._on_close = on_close

    def compose(self):
        yield Static(self._title, classes="title")
        yield _PaneClose(self._on_close)

    def set_title(self, title: str) -> None:
        self._title = title
        self.query_one(".title", Static).update(title)

    def set_active(self, active: bool) -> None:
        self.set_class(active, "-active")


class PaneCell(Vertical):
    """单格：标题栏 + EmbedPane。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    PaneCell {
        width: 1fr;
        height: 1fr;
        border: none;
        margin: 0 0 0 1;
        padding: 0;
    }
    PaneCell:first-child {
        margin-left: 0;
    }
    PaneCell EmbedPane {
        height: 1fr;
        margin: 0;
        padding: 0;
    }
    """

    def __init__(
        self,
        spec: PaneSpec,
        *,
        title: str,
        on_close: Callable[[], None],
        on_focus_list: Callable[[], None],
        osc_report: bytes | None,
        detail_renderer: Callable[[], Text | str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.spec = spec
        self._on_close = on_close
        self._on_focus_list = on_focus_list
        self._osc_report = osc_report
        self._title = title
        self._detail_renderer = detail_renderer

    def compose(self):
        yield _PaneHeader(self._title, self._on_close, classes="header")
        yield EmbedPane(
            on_focus_list=self._on_focus_list,
            osc_report=self._osc_report,
            id=f"embed-{self.spec.cell_id}",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._start_session)

    def _start_session(self) -> None:
        pane = self.embed_pane()
        if pane is None:
            return
        if self.spec.keepalive_name:
            pane.focus_session(
                self.spec.keepalive_name,
                self._detail_renderer,
            )
        elif self._detail_renderer is not None:
            pane.show_detail(self._detail_renderer)

    def embed_pane(self) -> EmbedPane | None:
        for child in self.children:
            if isinstance(child, EmbedPane):
                return child
        return None

    def _pane_header(self) -> _PaneHeader | None:
        """分栏重建/卸载过程中标题栏可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, _PaneHeader):
                return child
        return None

    def set_title(self, title: str) -> None:
        self._title = title
        header = self._pane_header()
        if header is not None:
            header.set_title(title)

    def focus_embed(self) -> None:
        pane = self.embed_pane()
        if pane is not None:
            pane.focus()

    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.call_after_refresh(self._sync_active_marker)

    def _on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self.call_after_refresh(self._sync_active_marker)

    def _sync_active_marker(self) -> None:
        # 双击顶栏助手、快速增删分栏时，焦点回调可能落在「标题栏尚未 compose
        # / 旧格已卸下」的中间态；真机复现：NoMatches: '_PaneHeader'。缺标题栏
        # 时静默跳过即可，下一轮焦点事件会再同步。
        header = self._pane_header()
        if header is None:
            return
        header.set_active(self.has_focus_within)


class SplitPaneArea(Vertical):
    """右侧：顶栏 + 动态 1~3 格。"""

    DEFAULT_CSS = """
    SplitPaneArea {
        width: 1fr;
        height: 1fr;
        margin: 0 0 0 1;
    }
    SplitPaneArea #pane-row {
        width: 1fr;
        height: 1fr;
    }
    SplitPaneArea #pane-row-empty {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        store,
        *,
        on_runtime_pick: Callable[[str], None],
        on_pane_close: Callable[[str], None],
        on_focus_list: Callable[[], None],
        osc_report: bytes | None = None,
        render_detail: Callable[[dict], Text] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.store = store
        self._on_runtime_pick = on_runtime_pick
        self._on_pane_close = on_pane_close
        self._on_focus_list = on_focus_list
        self._osc_report = osc_report
        self._render_detail = render_detail
        self.current_project: str = ""
        self._panes: list[PaneSpec] = []
        self._focus_key: str | None = None

    def compose(self):
        yield RuntimeTopBar(
            self.store.registry,
            self._on_runtime_pick,
            id="runtime-top-bar",
        )
        with Horizontal(id="pane-row"):
            yield Static(t("split.empty_hint"), id="pane-row-empty")

    def pane_count(self) -> int:
        return len(self._panes)

    def can_add_pane(self) -> bool:
        return len(self._panes) < MAX_PANES

    def any_embed_focused(self) -> bool:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                return True
        return False

    def host_pane_size(self) -> tuple[int, int]:
        """新建托管会话用的单格尺寸（主线程调用）。"""
        from pickup import embed as embed_mod

        row = self.query_one("#pane-row", Horizontal)
        count = max(1, len(self._panes) + 1)
        w = max(1, (row.size.width or 120) // count)
        h = max(1, row.size.height or 24)
        return embed_mod.normalize_host_size(w, h - 1)

    def invalidate_all_details(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.invalidate_detail()

    def scroll_preview_home(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_home()

    def scroll_preview_end(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_end()

    def scroll_preview_page(self, delta: int) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_page(delta)

    def close_focused_pane(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                self._close_spec(cell.spec)
                return
        if self._panes:
            self._close_spec(self._panes[-1])

    def remove_by_keepalive(self, keepalive_name: str) -> None:
        for spec in list(self._panes):
            if spec.keepalive_name == keepalive_name:
                self._close_spec(spec, notify=False)

    def show_new_session_hint(self) -> None:
        spec = PaneSpec(session_key="__hint__", cell_id=self._new_cell_id())
        self._panes = []
        self._schedule_mount(
            [(spec, {"source": "", "id": "__hint__", "fallback_title": ""}, lambda: Text(t("detail.new_session_hint")))],
        )

    def show_single_preview(
        self,
        session: dict,
        renderer: Callable[[], Text | str],
    ) -> None:
        key = make_session_key(session)
        import pickup

        project = pickup._normalize_cwd(session.get("cwd"))
        self.current_project = project
        spec = PaneSpec(session_key=key, cell_id=self._new_cell_id())
        self._schedule_mount(
            [(spec, session, renderer)],
            focus_key=key,
        )

    def show_hosted_group(
        self,
        project: str,
        entries: list[tuple[dict, str | None, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
    ) -> None:
        """entries: (session, keepalive_name, detail_renderer)"""
        self.current_project = project
        specs: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for session, kname, renderer in entries:
            key = make_session_key(session)
            spec = PaneSpec(session_key=key, keepalive_name=kname, cell_id=self._new_cell_id())
            specs.append((spec, session, renderer))
        self._panes = [s for s, _, _ in specs]
        self._focus_key = focus_key or (self._panes[0].session_key if self._panes else None)
        self._schedule_mount(
            [(s, sess, r) for s, sess, r in specs],
            focus_key=self._focus_key,
        )

    def add_hosted_pane(
        self,
        session: dict,
        keepalive_name: str,
        renderer: Callable[[], Text | str] | None,
        *,
        focus: bool = False,
    ) -> None:
        import pickup

        key = make_session_key(session)
        project = pickup._normalize_cwd(session.get("cwd"))
        if self.current_project and project and project != self.current_project:
            self.current_project = project
            self._panes = []
        elif not self.current_project:
            self.current_project = project
        spec = PaneSpec(session_key=key, keepalive_name=keepalive_name, cell_id=self._new_cell_id())
        existing = [(p, self._find_session(p.session_key)) for p in self._panes]
        rebuild: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for p, sess in existing:
            if sess is None:
                continue
            cell = self._cell_for_spec(p)
            renderer_fn = None
            if cell is not None:
                pane = cell.embed_pane()
                if pane is not None and not p.keepalive_name:
                    renderer_fn = pane._detail_renderer  # noqa: SLF001
            rebuild.append((p, sess, renderer_fn))
        rebuild.append((spec, session, renderer))
        self._panes = [s for s, _, _ in rebuild]
        focus_key = key if focus else self._focus_key
        self._schedule_mount(rebuild, focus_key=focus_key)

    def focus_session_key(self, session_key: str) -> None:
        for cell in self._cells():
            if cell.spec.session_key == session_key:
                cell.focus_embed()
                self._focus_key = session_key
                return

    def ordered_session_keys(self) -> list[str]:
        return [p.session_key for p in self._panes]

    def _cells(self) -> list[PaneCell]:
        row = self.query_one("#pane-row", Horizontal)
        return [c for c in row.children if isinstance(c, PaneCell)]

    def _cell_for_spec(self, spec: PaneSpec) -> PaneCell | None:
        for cell in self._cells():
            if cell.spec.cell_id == spec.cell_id:
                return cell
        return None

    def _find_session(self, key: str) -> dict | None:
        return self.store.find_session(key)

    def _new_cell_id(self) -> str:
        import uuid

        return uuid.uuid4().hex[:8]

    def _pane_title(self, session: dict) -> str:
        import pickup

        source = str(session.get("source") or "")
        if not source:
            return ""
        runtime = self.store.registry.get(source)
        title = self.store.get_title(session)
        return f"{runtime.display_name} · {title}"

    def _close_spec(self, spec: PaneSpec, *, notify: bool = True) -> None:
        self._panes = [p for p in self._panes if p.cell_id != spec.cell_id]
        if self._focus_key == spec.session_key:
            self._focus_key = self._panes[-1].session_key if self._panes else None
        if notify:
            self._on_pane_close(spec.session_key)
        if not self._panes:
            self.call_next(self._mount_panes_async, [])
            return
        rebuild = []
        for p in self._panes:
            sess = self._find_session(p.session_key)
            if sess is None:
                continue
            rebuild.append((p, sess, None))
        self.call_next(self._mount_panes_async, rebuild, focus_key=self._focus_key)

    def _schedule_mount(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
    ) -> None:
        self.call_next(self._mount_panes_async, entries, focus_key=focus_key)

    async def _mount_panes_async(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
    ) -> None:
        # remount 会弄丢焦点；列表原先有焦点时挂载后交回，避免 Enter 选不中
        focused = getattr(self.app, "focused", None)
        list_had_focus = focused is not None and (
            getattr(focused, "id", None) == "session-list"
            or type(focused).__name__ == "SessionListView"
        )
        row = self.query_one("#pane-row", Horizontal)
        await row.remove_children()
        if not entries:
            await row.mount(Static(t("split.empty_hint"), id="pane-row-empty"))
            self._panes = []
            if list_had_focus:
                self.call_after_refresh(self._on_focus_list)
            return
        self._panes = [s for s, _, _ in entries]
        for spec, session, renderer in entries:
            cell = PaneCell(
                spec,
                title=self._pane_title(session),
                on_close=lambda s=spec: self._close_spec(s),
                on_focus_list=self._on_focus_list,
                osc_report=self._osc_report,
                detail_renderer=renderer,
            )
            await row.mount(cell)
        if list_had_focus:
            self.call_after_refresh(self._on_focus_list)
        elif focus_key:
            self.call_after_refresh(lambda: self.focus_session_key(focus_key))
