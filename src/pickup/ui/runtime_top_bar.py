"""右侧助手顶栏：列出已安装运行时，点击派发新建托管会话。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.widget import Widget

if TYPE_CHECKING:
    from pickup.runtime.registry import RuntimeRegistry


class _RuntimeChip(Widget):
    """单个助手按钮。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _RuntimeChip {
        height: 1;
        width: auto;
        min-width: 8;
        padding: 0 1;
        margin: 0 1 0 0;
        content-align: center middle;
    }
    _RuntimeChip:hover {
        background: $primary-darken-2;
    }
    """

    def __init__(
        self,
        runtime_id: str,
        label: str,
        style: str,
        on_pick: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.runtime_id = runtime_id
        self._label = label
        self._style = style
        self._on_pick = on_pick

    def render(self) -> Text:
        return Text(self._label, style=self._style)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_pick(self.runtime_id)


class RuntimeTopBar(Horizontal):
    """右侧顶栏：已安装助手均可点击。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    RuntimeTopBar {
        height: 1;
        width: 1fr;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """

    def __init__(
        self,
        registry: RuntimeRegistry,
        on_runtime_pick: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._registry = registry
        self._on_runtime_pick = on_runtime_pick

    def compose(self):
        import pickup

        for runtime in self._registry:
            if not runtime.is_available():
                continue
            yield _RuntimeChip(
                runtime.id,
                runtime.display_name,
                pickup.runtime_label_style(runtime.id),
                self._on_runtime_pick,
                id=f"runtime-chip-{runtime.id}",
            )
