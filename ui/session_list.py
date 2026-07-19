"""会话列表：左栏两行卡片（标题行 + 状态/时间行），取代旧版 curses 手绘表格。

业务格式化逻辑（相对时间、宽字符对齐、标题兜底）直接复用 pickup.py 里已测试的
纯函数，这里只负责「怎么在 Textual 里画卡片、怎么响应选择」。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import ListItem, ListView

if TYPE_CHECKING:
    import pickup


NEW_SESSION_ID = "__new_session__"


class SessionCard(Widget):
    """单个会话的两行卡片：标题（含项目前缀/生成中转圈圈）+ 状态/来源/相对时间。"""

    # Textual 默认所有 Widget 都允许鼠标拖拽文本选择（ALLOW_SELECT=True）；这类
    # 卡片是"点击=选中该会话"的列表项，不是可选文本内容，必须关掉——否则鼠标
    # 点击会触发 Textual 内置的 SelectStart 逻辑，在 ListView 卡片这种没有常规
    # 可滚动祖先的场景下，container 解析为 None 后访问 .region 直接崩溃退出
    # （真机实测复现：点击会话卡直接闪退，AttributeError: 'NoneType' object
    # has no attribute 'region'）。
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionCard {
        height: 2;
        width: 1fr;
    }
    """

    def __init__(self, session: dict, store: "pickup.SessionStore", spin_char: str) -> None:
        super().__init__()
        self.session = session
        self._store = store
        self._spin_char = spin_char

    def render(self) -> Text:
        import pickup  # 延迟导入：ui 包只在 pickup.main() 运行期才加载，届时模块已就绪

        session = self.session
        store = self._store
        key = pickup.session_key(session)
        display_titles, generating = store.snapshot()
        title = display_titles.get(key, session["fallback_title"])
        is_gen = key in generating
        is_keepalive = bool(session.get("keepalive_name"))
        status_text = "运行中(托管)" if is_keepalive else ("运行中" if session.get("live") else "已结束")

        project_path = pickup._normalize_cwd(session.get("cwd"))
        project = os.path.basename(project_path) if project_path else str(session.get("cwd_display") or "未知项目")
        title_prefix = f"{project}: "
        if is_gen:
            title_prefix = f"{self._spin_char} {title_prefix}"
        width = max(10, self.size.width or 40)
        title_line = pickup._fit_cell(title_prefix + title, width)

        runtime_name = store.registry.get(str(session.get("source") or "")).display_name
        meta_left = f"{status_text} · {runtime_name}"
        relative_time = pickup._format_relative_time(session.get("mtime") or 0)
        meta_width = max(1, width - pickup._text_width(relative_time))
        meta_line = pickup._fit_cell(meta_left, meta_width) + pickup._fit_cell_right(relative_time, width - meta_width)

        out = Text(title_line, style="bold" if is_gen else "")
        out.append("\n")
        out.append(meta_line, style="dim")
        return out


class NewSessionCard(Widget):
    """列表顶部固定的「新建会话」项，恒排第一，不参与滚动区排序。"""

    ALLOW_SELECT = False  # 原因同 SessionCard：点击这项是选中动作，不是选文本

    DEFAULT_CSS = """
    NewSessionCard {
        height: 1;
        width: 1fr;
    }
    """

    def render(self) -> Text:
        return Text("＋ 新建会话", style="bold")


class SessionListView(ListView):
    """会话列表：虚拟索引 0 固定为新建会话项，之后是稳定顺序的会话卡片。"""

    BINDINGS = [
        Binding("j", "cursor_down", "选择", show=False),
        Binding("k", "cursor_up", "选择", show=False),
    ]

    def __init__(self, store: "pickup.SessionStore", nav, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        # 项目筛选状态只认 nav.project_key 这一份：曾经在这里另开一个
        # self.project_key 属性，cycle_project_filter 只改了这份、
        # MainScreen._update_header 却读 nav.project_key，两边不同步导致
        # 页头筛选文案和实际筛选结果对不上（真机排查发现的真实 bug）。
        self.nav = nav
        self._spin_frame = 0

    async def on_mount(self) -> None:
        await self.rebuild()
        self.set_interval(0.15, self._tick_spinner)

    def _tick_spinner(self) -> None:
        import pickup

        _, generating = self.store.snapshot()
        if not generating:
            return
        self._spin_frame += 1
        spin_char = pickup.SPINNER_FRAMES[self._spin_frame % len(pickup.SPINNER_FRAMES)]
        for item in self.children:
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard):
                card._spin_char = spin_char
                card.refresh()

    def visible_sessions(self) -> list[dict]:
        import pickup

        return pickup._filter_sessions(self.store.all_sessions(), self.nav.project_key)

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
        """按当前筛选重建全部条目；尽量保持原有选中的会话不变（后台重扫后调用）。"""
        import pickup

        previous_key = None
        if keep_selection:
            selected = self.selected_session()
            if selected is not None:
                previous_key = pickup.session_key(selected)

        sessions = self.visible_sessions()
        await self.clear()
        await self.append(ListItem(NewSessionCard(), id=NEW_SESSION_ID))

        new_index = 0
        for i, session in enumerate(sessions):
            item = ListItem(SessionCard(session, self.store, pickup.SPINNER_FRAMES[0]))
            await self.append(item)
            if previous_key is not None and pickup.session_key(session) == previous_key:
                new_index = i + 1
        if previous_key is not None:
            self.index = new_index
        elif self.index is None:
            self.index = 1 if sessions else 0

    async def cycle_project_filter(self) -> None:
        keys = [None, *(project["cwd_key"] for project in self.store.projects())]
        try:
            position = keys.index(self.nav.project_key)
        except ValueError:
            position = 0
        self.nav.project_key = keys[(position + 1) % len(keys)]
        await self.rebuild(keep_selection=False)
        self.index = 1 if len(self.visible_sessions()) else 0
