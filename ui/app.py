"""PickupApp：Textual 应用外壳，取代 curses.wrapper(_run, ...) 的入口点。

`run_app()` 是唯一对外函数，返回值语义与旧版完全一致：
`LaunchRequest | NewSessionRequest` 表示需要外层 execvp 全屏接管，
`None` 表示用户只是退出。
"""

from __future__ import annotations

from textual.app import App

from ui.main_screen import MainScreen


class PickupApp(App):
    """pickup 的主应用；具体界面全部在 MainScreen 里，这里只挂屏幕。"""

    TITLE = "pickup"
    # 不整体关闭 Textual 内置的鼠标拖拽文本选择：EmbedPane 需要它来实现"划词
    # 选中托管会话画面里的文字 + Ctrl+C 复制"（老版 curses 用手写 OSC 52 做的
    # 事，这版直接用 Textual 自带机制）。真机实测过的崩溃只出在 SessionCard/
    # NewSessionCard 这类会被后台重扫动态增删的列表项 Widget 上（选择过程中
    # 控件被移除，container 解析为 None 后访问 .region 崩溃），已单独在那两个
    # 类和弹窗菜单项上关闭 ALLOW_SELECT；EmbedPane 画面不会被动态增删，不受
    # 这条限制，保留选择能力。
    CSS = """
    #list-header {
        height: 1;
        color: white;
        background: $primary-darken-2;
    }

    #session-list > ListItem {
        margin-bottom: 1;
    }
    """

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        self._store = store
        self._embed_ok = embed_ok
        self._direct = direct
        self._osc_report = osc_report

    def _on_terminal_supports_synchronized_output(self, message) -> None:
        # Textual 默认在终端支持时把每帧包进同步输出（`\e[?2026h…l`，原子提交整帧防撕裂）。
        # 排查"iTerm2 + SSH 下输入法候选词小窗跑到左上角"时怀疑过这层：SSH 有网络
        # 延迟/分片，合成期间 iTerm2 有概率读到同步块里尚未提交的旧光标位置（初始
        # 左上角）。设 PICKUP_DISABLE_SYNC_OUTPUT=1 可保持 _sync_available=False、
        # 不再包同步块，用于验证/规避该现象（代价是大面积重绘可能有轻微闪烁）。
        import os

        if os.environ.get("PICKUP_DISABLE_SYNC_OUTPUT") == "1":
            return
        super()._on_terminal_supports_synchronized_output(message)

    def on_mount(self) -> None:
        # pickup 自己的界面色也要跟随外层终端的深浅色，不能只管注入托管 pane
        # 那一份——两者是分开的：这里管 pickup 自身列表/页头/弹窗的配色，
        # embed_pane.py 的 report_theme 管托管会话自己的深浅色检测。此前完全
        # 没接这一段，Textual 默认主题在浅色终端下会显得配色不对（真机实测发现）。
        import pickup
        is_light = pickup._background_is_light(self._osc_report)
        if is_light is not None:
            self.theme = "textual-light" if is_light else "textual-dark"
        self.push_screen(MainScreen(self._store, self._embed_ok, self._direct, self._osc_report))


def run_app(store, embed_ok: bool, direct=None, osc_report: bytes | None = None):
    """启动 Textual 界面并阻塞直至用户退出或选择启动某个会话。

    返回值与旧版 `curses.wrapper(_run, store, embed_ok, direct)` 语义一致：
    `LaunchRequest | NewSessionRequest | None`。osc_report 是启动前探测到的外层
    终端 OSC 10/11 应答（见 pickup._probe_osc_colours），用于内嵌面板聚焦托管
    会话时注入真实背景色。
    """
    app = PickupApp(store, embed_ok, direct, osc_report)
    return app.run()
