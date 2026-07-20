"""右下角新版本浮层：docked 定位容器 + 内部可点击主体。

外层 UpdateToast 用 `dock: bottom` + `align: right bottom` 把自己锚到屏幕右下角
（镜像 Textual 内置 Toast/ToastRack 的定位手法：单个 leaf widget 自身 docked 时
无法把自己右对齐——`align` 只作用于容器的子节点，所以外层必须是容器，真正的
可点击主体是内部子节点）。不挂进 `#list-pane`，不受侧边栏末行间隔的硬约定牵连。

状态机：hidden → available(version) → updating → done(version) / failed。
点击主体按当前状态触发不同回调；仅 available 状态下额外露出一个"忽略"命中区。
"""

from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual import events
from textual.containers import Container, Horizontal
from textual.widgets import Static

from pickup.display import SPINNER_FRAMES
from pickup.i18n import t

_SPIN_INTERVAL = 0.08  # 秒，与列表转圈圈一致的观感


class _ToastBody(Static):
    """浮层主体：展示文案，点击触发当前状态对应的主动作。"""

    ALLOW_SELECT = False

    def __init__(self, on_click: Callable[[], None], **kwargs) -> None:
        super().__init__("", **kwargs)
        self._on_click = on_click

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_click()


class _ToastClose(Static):
    """浮层的"忽略"小命中区，只在 available 状态下挂载。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _ToastClose {
        width: 3;
        height: auto;
        border: round $primary;
        border-left: none;
        content-align: center middle;
        color: $text-muted;
    }
    _ToastClose:hover {
        color: $text;
        background: $primary-darken-1;
    }
    """

    def __init__(self, on_click: Callable[[], None], **kwargs) -> None:
        super().__init__(t("update.dismiss"), **kwargs)
        self._on_click = on_click

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_click()


class UpdateToast(Container):
    """右下角新版本提示；MainScreen 通过 show_*/hide 驱动状态，通过构造参数里的
    四个回调获知用户点击了什么。"""

    DEFAULT_CSS = """
    UpdateToast {
        layer: overlay;
        dock: bottom;
        width: 1fr;
        height: auto;
        align: right bottom;
        display: none;
    }
    UpdateToast.-visible {
        display: block;
    }
    UpdateToast Horizontal {
        width: auto;
        height: auto;
        margin: 0 2 1 0;
    }
    UpdateToast _ToastBody {
        width: auto;
        height: auto;
        max-width: 60;
        border: round $primary;
        background: $panel;
        padding: 0 1;
        color: $text;
    }
    UpdateToast _ToastBody.-failed {
        border: round $error;
    }
    UpdateToast _ToastBody.-done {
        border: round $success;
    }
    UpdateToast _ToastBody:hover {
        background: $primary-darken-1;
    }
    """

    def __init__(
        self,
        *,
        on_update: Callable[[], None],
        on_restart: Callable[[], None],
        on_retry: Callable[[], None],
        on_dismiss: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._on_update = on_update
        self._on_restart = on_restart
        self._on_retry = on_retry
        self._on_dismiss = on_dismiss
        self._state = "hidden"  # hidden | available | updating | done | failed
        self._version = ""
        self._spin_index = 0
        self._spin_timer = None

    def compose(self):
        with Horizontal():
            yield _ToastBody(self._handle_body_click, id="toast-body")
            yield _ToastClose(self._handle_close_click, id="toast-close")

    # ---- 对外状态切换入口，MainScreen 在后台 worker 回调里调用 ----

    def show_available(self, version: str) -> None:
        self._version = version
        self._set_state("available")

    def show_updating(self) -> None:
        self._set_state("updating")
        self._start_spin()

    def show_done(self, version: str) -> None:
        self._version = version
        self._stop_spin()
        self._set_state("done")

    def show_failed(self) -> None:
        self._stop_spin()
        self._set_state("failed")

    def hide(self) -> None:
        self._stop_spin()
        self._set_state("hidden")

    # ---- 内部 ----

    def _set_state(self, state: str) -> None:
        self._state = state
        if not self.is_mounted:
            return
        self._render()

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        self.set_class(self._state != "hidden", "-visible")
        close = self.query_one("#toast-close", _ToastClose)
        close.display = self._state == "available"
        body = self.query_one("#toast-body", _ToastBody)
        body.remove_class("-failed", "-done")
        if self._state == "available":
            body.update(Text(t("update.available", version=self._version)))
        elif self._state == "updating":
            frame = SPINNER_FRAMES[self._spin_index % len(SPINNER_FRAMES)]
            body.update(Text(f"{frame} {t('update.updating')}"))
        elif self._state == "done":
            body.add_class("-done")
            body.update(Text(t("update.done_restart", version=self._version)))
        elif self._state == "failed":
            body.add_class("-failed")
            body.update(Text(t("update.failed_retry")))

    def _start_spin(self) -> None:
        self._stop_spin()
        self._spin_index = 0
        self._spin_timer = self.set_interval(_SPIN_INTERVAL, self._tick_spin)

    def _tick_spin(self) -> None:
        self._spin_index += 1
        if self._state == "updating":
            self._render()

    def _stop_spin(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _handle_body_click(self) -> None:
        if self._state == "available":
            self._on_update()
        elif self._state == "done":
            self._on_restart()
        elif self._state == "failed":
            self._on_retry()
        # updating 状态点击无动作，避免重复触发

    def _handle_close_click(self) -> None:
        if self._state == "available":
            self._on_dismiss(self._version)
