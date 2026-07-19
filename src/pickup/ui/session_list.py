"""会话列表：左栏会话卡片 + 顶部新建项，取代旧版 curses 手绘表格。

侧边栏布局硬约定（凡往左栏加控件都必须遵守，见 AGENTS.md / MAINTAINER_GUIDE）：
每个块的最后一行是间隔空行，画在控件自身高度内并算进命中区；禁止用 margin
或兄弟空隙做分隔。当前：搜索框高 2、新建项高 2、会话卡高 3。

业务格式化逻辑（相对时间、宽字符对齐、标题兜底）直接复用 pickup.py 里已测试的
纯函数，这里只负责「怎么在 Textual 里画卡片、怎么响应选择」。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import ListItem, ListView

if TYPE_CHECKING:
    import pickup


NEW_SESSION_ID = "__new_session__"


class SessionCard(Widget):
    """会话卡片：两行正文 + 末行间隔（总高 3）。"""

    # Textual 默认所有 Widget 都允许鼠标拖拽文本选择（ALLOW_SELECT=True）；这类
    # 卡片是"点击=选中该会话"的列表项，不是可选文本内容，必须关掉——否则鼠标
    # 点击会触发 Textual 内置的 SelectStart 逻辑，在 ListView 卡片这种没有常规
    # 可滚动祖先的场景下，container 解析为 None 后访问 .region 直接崩溃退出
    # （真机实测复现：点击会话卡直接闪退，AttributeError: 'NoneType' object
    # has no attribute 'region'）。
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionCard {
        height: 3;
        width: 1fr;
    }
    """

    def __init__(
        self,
        session: dict,
        store: "pickup.SessionStore",
        spin_char: str,
        *,
        display_title: str | None = None,
        is_generating: bool = False,
    ) -> None:
        super().__init__()
        self.session = session
        self._store = store
        self._spin_char = spin_char
        # 展示标题/生成中标记由外部（rebuild()/_tick_spinner）注入并按需更新，
        # 不在 render() 里自己调用 store.snapshot()——那个方法要拿锁、拷贝整个
        # display_titles dict 和 generating set，卡片一多就是重复的拷贝开销。
        self.display_title = display_title if display_title is not None else session["fallback_title"]
        self.is_generating = is_generating
        self._render_signature = self._compute_signature()

    def _compute_signature(self) -> tuple:
        """渲染相关字段的轻量快照，用来判定"内容是否真的变了"、要不要 refresh()。"""
        session = self.session
        return (
            self.display_title,
            self.is_generating,
            bool(session.get("live")),
            session.get("keepalive_name"),
            session.get("mtime"),
        )

    def apply_update(self, session: dict, display_title: str, is_generating: bool) -> bool:
        """原地更新路径专用：替换会话引用与展示态，仅当渲染相关字段确实变化
        时才 refresh()。返回是否触发了 refresh，供调用方按需断言/统计。"""
        self.session = session
        self.display_title = display_title
        self.is_generating = is_generating
        signature = self._compute_signature()
        changed = signature != self._render_signature
        self._render_signature = signature
        if changed:
            self.refresh()
        return changed

    def render(self) -> Text:
        import pickup  # 延迟导入：ui 包只在 pickup.main() 运行期才加载，届时模块已就绪

        session = self.session
        store = self._store
        title = self.display_title
        is_gen = self.is_generating
        is_keepalive = bool(session.get("keepalive_name"))
        is_running = is_keepalive or bool(session.get("live"))
        from pickup.i18n import t

        status_text = (
            t("status.running_hosted")
            if is_keepalive
            else (t("status.running") if is_running else t("status.ended"))
        )

        project_path = pickup._normalize_cwd(session.get("cwd"))
        project = (
            os.path.basename(project_path)
            if project_path
            else str(session.get("cwd_display") or t("project.unknown"))
        )
        title_prefix = f"{project}: "
        if is_gen:
            title_prefix = f"{self._spin_char} {title_prefix}"
        width = max(10, self.size.width or 40)

        runtime = store.registry.get(str(session.get("source") or ""))
        runtime_name = runtime.display_name
        runtime_id = getattr(runtime, "id", None) or str(session.get("source") or "")
        runtime_width = min(width - 1, max(1, pickup._text_width(runtime_name)))
        title_width = width - runtime_width
        title_cell = pickup._fit_cell(title_prefix + title, title_width, ellipsis=True)
        runtime_cell = pickup._fit_cell_right(runtime_name, runtime_width)

        relative_time = pickup._format_relative_time(session.get("mtime") or 0)
        time_width = min(width - 1, max(1, pickup._text_width(relative_time)))
        status_width = width - time_width
        status_cell = pickup._fit_cell(status_text, status_width)
        time_cell = pickup._fit_cell_right(relative_time, time_width)

        out = Text(title_cell)
        out.append(runtime_cell, style=pickup.runtime_label_style(runtime_id))
        out.append("\n")
        out.append(status_cell, style="green" if is_running else "dim")
        out.append(time_cell, style="dim")
        # 第三行空行：视觉分隔，同时算进本卡命中区（不要用 ListItem margin/padding）
        out.append("\n")
        return out


class NewSessionCard(Widget):
    """列表顶部「新建会话」：一行正文 + 末行间隔（总高 2）。"""

    ALLOW_SELECT = False  # 原因同 SessionCard：点击这项是选中动作，不是选文本

    DEFAULT_CSS = """
    NewSessionCard {
        height: 2;
        width: 1fr;
    }
    """

    def render(self) -> Text:
        from pickup.i18n import t

        # 第二行空行：与会话卡同样把分隔算进本项命中区
        return Text(t("list.new_session"), style="bold") + Text("\n")


class SessionListView(ListView):
    """会话列表：虚拟索引 0 固定为新建会话项，之后是稳定顺序的会话卡片。"""

    # 隐藏滚动条占位，保留键盘/滚轮滚动（scrollbar-size: 0 不关掉 overflow）。
    DEFAULT_CSS = """
    SessionListView {
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Select", show=False),
        Binding("k", "cursor_up", "Select", show=False),
        # 覆盖 ScrollableContainer 的 up/down=scroll_*：会话列表应移光标，不是滚视口
        Binding("down", "cursor_down", "Select", show=False),
        Binding("up", "cursor_up", "Select", show=False),
    ]

    def __init__(self, store: "pickup.SessionStore", nav, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        # 项目搜索查询只认 nav.project_query 这一份，供 visible_sessions /
        # 页头占位文案 / 新建会话目录解析共用，禁止在本类另开一份状态。
        self.nav = nav
        self._spin_frame = 0

    async def on_mount(self) -> None:
        await self.rebuild()
        self.set_interval(0.15, self._tick_spinner)

    def _tick_spinner(self) -> None:
        # 便宜检查在前：没有任何会话在生成标题时，连 store.snapshot() 都不调——
        # 后者要拿锁、拷贝 display_titles dict 和 generating set，150ms 一次的
        # 轮询白拷贝纯属浪费。有会话在生成时也只刷新命中 generating 的那几张
        # 卡片，不遍历全部子项逐个 refresh。
        if not self.store.has_generating():
            return
        import pickup

        display_titles, generating = self.store.snapshot()
        self._spin_frame += 1
        spin_char = pickup.SPINNER_FRAMES[self._spin_frame % len(pickup.SPINNER_FRAMES)]
        for card in self._session_cards():
            key = pickup.session_key(card.session)
            if key not in generating:
                continue
            card._spin_char = spin_char
            card.display_title = display_titles.get(key, card.session["fallback_title"])
            card.is_generating = True
            card.refresh()

    def _session_cards(self) -> list[SessionCard]:
        """按当前显示顺序返回全部 SessionCard（跳过顶部固定的新建会话项）。"""
        cards = []
        for item in self.children:
            if item.id == NEW_SESSION_ID:
                continue
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard):
                cards.append(card)
        return cards

    def _current_session_keys(self) -> list[str]:
        import pickup

        return [pickup.session_key(card.session) for card in self._session_cards()]

    def _update_cards_in_place(self, sessions: list[dict]) -> None:
        """会话集合（顺序+成员）没变，只需换 SessionCard 手上的 session 引用、
        按需 refresh，不碰 ListView 子项结构（不 mount/unmount 任何 Widget）。"""
        import pickup

        display_titles, generating = self.store.snapshot()
        for card, session in zip(self._session_cards(), sessions):
            key = pickup.session_key(session)
            card.apply_update(session, display_titles.get(key, session["fallback_title"]), key in generating)

    def visible_sessions(self) -> list[dict]:
        import pickup

        display_titles, _ = self.store.snapshot()
        return pickup._filter_sessions_by_query(
            self.store.all_sessions(),
            self.nav.project_query,
            titles=display_titles,
        )

    def selected_session(self) -> dict | None:
        sessions = self.visible_sessions()
        idx = self.index
        if idx is None or idx == 0:
            return None
        pos = idx - 1
        return sessions[pos] if 0 <= pos < len(sessions) else None

    def is_new_session_selected(self) -> bool:
        return self.index == 0

    async def rebuild(self, *, keep_selection: bool = True) -> None:
        """按当前筛选重建条目；尽量保持原有选中的会话不变（后台重扫后调用）。

        会话集合（顺序+成员）没变时走原地更新——只换 SessionCard 手上的
        session 引用、按需 refresh()，不碰 ListView 子项结构；集合真的变了
        （新增/删除/顺序变化）才走批量清空重建，见 docs/MAINTAINER_GUIDE.md
        「界面」节的性能优化记录。
        """
        import pickup

        previous_key = None
        if keep_selection:
            selected = self.selected_session()
            if selected is not None:
                previous_key = pickup.session_key(selected)

        sessions = self.visible_sessions()
        new_keys = [pickup.session_key(session) for session in sessions]
        t0 = time.perf_counter()

        if new_keys == self._current_session_keys():
            self._update_cards_in_place(sessions)
            if previous_key is None and self.index is None:
                self.index = 1 if sessions else 0
            from pickup import observe
            observe.event(
                "list_rebuild",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                mode="in_place",
                card_count=len(sessions),
            )
            return

        display_titles, generating = self.store.snapshot()
        items = [ListItem(NewSessionCard(), id=NEW_SESSION_ID)]
        for session in sessions:
            key = pickup.session_key(session)
            items.append(
                ListItem(
                    SessionCard(
                        session,
                        self.store,
                        pickup.SPINNER_FRAMES[0],
                        display_title=display_titles.get(key, session["fallback_title"]),
                        is_generating=key in generating,
                    )
                )
            )

        # clear 前记下是否已有会话卡：用来区分「初次填充」和「用户正停在新建项」
        had_session_cards = bool(self._session_cards())

        # batch_update() 抑制 clear()+extend() 中间那次多余重绘；两步都要 await
        # 完成（DOM 真正更新），批量 API 本身已经把"多次 mount"合成一轮。
        with self.app.batch_update():
            await self.clear()
            await self.extend(items)

        new_index = 0
        for i, session in enumerate(sessions):
            if previous_key is not None and pickup.session_key(session) == previous_key:
                new_index = i + 1
        if previous_key is not None:
            self.index = new_index
        elif not had_session_cards:
            # 初次填充：默认选最近一条会话（进 pickup 回车即恢复）
            self.index = 1 if sessions else 0
        # Textual 已知问题（issue #6300）：clear()+extend() 后紧接着设置 index，
        # 高亮理论上可能只在内部状态里正确、要等用户交互才真正刷新到屏幕。在当前
        # 锁定版本（8.2.8）下用 Pilot 直接探查过 compositor 的增量重绘路径，没有
        # 复现出"选中但不刷新"的现象——但探查手段本身有局限（无法完全模拟真实
        # 终端的部分重绘时序），显式 refresh() 成本几乎为零，保留作为兜底不会有
        # 副作用，直接加上。
        self.refresh()
        from pickup import observe
        observe.event(
            "list_rebuild",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            mode="full",
            card_count=len(sessions),
        )
