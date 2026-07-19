"""通用选择/确认弹窗：取代旧版 curses 手绘的 _pick_menu / _draw_runtime_menu /
_confirm_kill_keepalive。业务规则（运行时可用性、默认高亮项、文案）保持不变，
只是从「手画方框 + 内部按键循环」换成 Textual 的 ModalScreen + ListView。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static


class _ChoiceItem(Static):
    # 菜单项文字没有选择/复制的使用场景，关掉避免和 SessionCard 同类的潜在风险
    # （见 ui/app.py 里 PickupApp 的说明）。
    ALLOW_SELECT = False

    def __init__(self, main: str, hint: str, available: bool) -> None:
        style = "" if available else "dim"
        text = Text(main, style=style)
        if hint:
            text.append("  " + hint, style="dim")
        super().__init__(text)
        self.available = available


class PickMenuModal(ModalScreen[int | None]):
    """居中单选弹窗：entries 为 (主文案, 副文案[, 是否可选=True])。返回选中下标或 None。"""

    DEFAULT_CSS = """
    PickMenuModal {
        align: center middle;
    }
    PickMenuModal > Vertical {
        width: auto;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 0 1;
    }
    PickMenuModal ListView {
        height: auto;
        max-height: 20;
    }
    """

    def __init__(self, title: str, entries: list[tuple], default_index: int = 0) -> None:
        super().__init__()
        self._title = title
        self._entries = entries
        self._default_index = default_index

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f" {self._title} ", classes="title")
            items = []
            for entry in self._entries:
                main, hint = entry[0], entry[1]
                available = entry[2] if len(entry) > 2 else True
                items.append(ListItem(_ChoiceItem(main, hint, available)))
            yield ListView(*items, initial_index=self._default_index)
            yield Label("↑↓ 选择   Enter 确认   Esc 返回", classes="hint")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        choice = event.item.children[0]
        if isinstance(choice, _ChoiceItem) and not choice.available:
            self.app.bell()
            return
        list_view = self.query_one(ListView)
        self.dismiss(list_view.index)

    def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


@dataclass
class RuntimeChoice:
    id: str
    label: str
    action_text: str
    available: bool


class RuntimePickerModal(ModalScreen[str | None]):
    """运行时选择弹窗（高级操作接力 / 新建会话选运行时共用）。返回 runtime id 或 None。"""

    DEFAULT_CSS = PickMenuModal.DEFAULT_CSS.replace("PickMenuModal", "RuntimePickerModal")

    def __init__(self, title: str, choices: list[RuntimeChoice], default_index: int = 0) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._default_index = default_index

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f" {self._title} ", classes="title")
            items = []
            for choice in self._choices:
                action = choice.action_text if choice.available else f"{choice.action_text}［未安装］"
                items.append(ListItem(_ChoiceItem(f"{choice.label:<10}", action, choice.available)))
            yield ListView(*items, initial_index=self._default_index)
            yield Label("↑↓ 选择   Enter 确认   Esc 返回", classes="hint")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_view = self.query_one(ListView)
        index = list_view.index
        if index is None:
            return
        choice = self._choices[index]
        if not choice.available:
            self.app.bell()
            return
        self.dismiss(choice.id)

    def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """q 确认 / 其他键取消的确认框，取代 _confirm_kill_keepalive。

    打开瞬间会短暂忽略按键：结束会话本身由 `q` 触发，若同一按键落到弹窗里会
    立刻被当成确认。挂载后等一帧再接收确认/取消。
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: auto;
        max-width: 80%;
        height: auto;
        border: round $warning;
        padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message
        self._armed = False

    def compose(self) -> ComposeResult:
        from i18n import t

        with Vertical():
            yield Label(self._message)
            yield Label(t("modal.confirm_hint"), classes="hint")

    def on_mount(self) -> None:
        self.call_after_refresh(self._arm)

    def _arm(self) -> None:
        self._armed = True

    def _on_key(self, event: events.Key) -> None:
        event.stop()
        if not self._armed:
            return
        self.dismiss(event.key in ("q", "Q"))


# ---------------------------------------------------------------------------
# 业务流程封装：project/runtime 选择 + 新建会话组合流程
# ---------------------------------------------------------------------------

async def choose_target_runtime(app, store, source: str) -> str | None:
    """高级操作：选择接力目标运行时（复用 pickup._choose_target_runtime 的业务规则）。"""
    runtimes = list(store.registry)
    source_name = store.registry.get(source).display_name
    choices = []
    for runtime in runtimes:
        if runtime.id == source:
            action = "原生恢复（保留完整上下文）"
        else:
            action = f"读取 {source_name} 历史后新建会话"
        choices.append(RuntimeChoice(runtime.id, runtime.display_name, action, runtime.is_available()))
    default_index = next(
        (i for i, runtime in enumerate(runtimes) if runtime.id != source and runtime.is_available()),
        next((i for i, runtime in enumerate(runtimes) if runtime.id == source), 0),
    )
    return await app.push_screen_wait(
        RuntimePickerModal("高级操作：选择接力运行时", choices, default_index)
    )


async def pick_runtime_for_new_session(app, store, default_id: str) -> str | None:
    runtimes = list(store.registry)
    choices = [
        RuntimeChoice(runtime.id, runtime.display_name, "在该目录下新建空白会话", runtime.is_available())
        for runtime in runtimes
    ]
    default_index = next(
        (i for i, runtime in enumerate(runtimes) if runtime.id == default_id and runtime.is_available()),
        next((i for i, runtime in enumerate(runtimes) if runtime.is_available()), 0),
    )
    return await app.push_screen_wait(
        RuntimePickerModal("新建会话：选择运行时", choices, default_index)
    )


async def pick_project(app, store, nav, session: dict | None) -> str | None:
    import pickup

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for project in store.projects():
        cwd_key = project["cwd_key"]
        if not cwd_key or cwd_key in seen:
            continue
        seen.add(cwd_key)
        entries.append((cwd_key, project["label"], cwd_key))
    current = os.getcwd()
    if current not in seen:
        entries.insert(0, (current, "当前目录", current))
    if not entries:
        return None
    preferred = pickup._new_session_cwd(store, nav, session)
    default_index = next(
        (i for i, (cwd, _, _) in enumerate(entries) if preferred and cwd == preferred), 0
    )
    picked = await app.push_screen_wait(
        PickMenuModal("新建会话：选择项目", [(label, hint) for _, label, hint in entries], default_index)
    )
    if picked is None:
        return None
    return pickup.usable_cwd(entries[picked][0])


async def new_session_flow(app, store, nav, session: dict | None):
    import pickup

    cwd = await pick_project(app, store, nav, session)
    if cwd is None:
        return None
    target = await pick_runtime_for_new_session(app, store, nav.source)
    if target is None:
        return None
    return pickup.NewSessionRequest(target, cwd)
