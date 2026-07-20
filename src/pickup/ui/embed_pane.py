"""EmbedPane：把托管在 tmux 里的会话画面渲染进 Textual 界面的自定义 widget。

沿用 embed.py 的抓帧/控制通道/按键翻译（与 UI 框架无关，旧 curses 版本同样调用
这些函数）；本模块只负责「怎么在 Textual 里画出来、怎么把 Textual 的按键/鼠标/
粘贴事件转发给 tmux」，取代旧版 pickup.py 里的 _draw_embed_pane + 主循环中一大段
手写 SGR 鼠标解析。

抓帧必须跑在后台线程：`embed.capture`/`embed.pane_state` 都是同步 fork 一个
tmux 客户端进程的阻塞调用（真机实测：滚轮/鼠标事件处理函数如果直接同步调用
这两个函数，每次滚动都会卡住整个 Textual 事件循环，表现为内嵌面板滚动极其
卡顿）。这里的设计对应旧版 curses 主循环里的 `_capture_loop` 后台线程 +
`emb.mouse_any`/`emb.history_size` 等缓存字段：后台线程独享 tmux 查询，
主线程的按键/滚轮处理只读缓存、不发起新的阻塞调用。

滚动/选词说明：tmux 回滚（history_offset）与 pickup 主屏的按键滚动完全保留；
鼠标拖拽选词直接复用 Textual 内置的文本选择（未设 ALLOW_SELECT=False，见类
docstring），取代旧版 curses 手写的框选高亮 + OSC 52 复制。

Ctrl+C 语义特别说明：Textual 的 Screen 基类把 ctrl+c 全局绑定为
"复制当前选中文本"（screen.copy_text），而按键的 BINDINGS 检查发生在事件
派发到本 widget 的 on_key 之前——如果不在这里也接管 ctrl+c，聚焦本面板时
按 Ctrl+C 永远会被"复制选中文本"吞掉，永远发不到托管会话里，而 Ctrl+C 中断
一个正在运行的命令是终端最基本的操作，必须保留。这里的策略：有选中文本就
复制（复刻 Screen.action_copy_text 的行为），没有选中文本就照常转发给
托管会话，两者按当前状态二选一，不会互相打架。

渲染改用 Textual 的 Line API（`render_line`，不是 `render`）：托管会话画面
每帧只有极少数行真的变了（典型场景是敲字/输出滚动，光标附近一两行变化，
其余行原样不动），旧版 `render()` 每帧都要重新遍历全部行、重建整个
`rich.text.Text` 对象，是内嵌面板 CPU 占用的另一个主要来源（抓帧本身的
fork 开销见上面的说明；这里是抓完之后、画到屏幕前的开销）。改法：
`_sync_strips` 按行比对新旧 `embed.Cell` 网格，只对真正变化的行重新编译
`textual.strip.Strip`（`embed.row_text_and_spans` 提供跟旧版整屏路径共用的
合并算法，避免两处独立实现分叉），只对这些行调用 `self.refresh(Region)`
局部刷新——Textual 官方文档确认 `render_line` 只会为落在 dirty region 内的
行被调用，这正是相对整屏 `render()` 的实际收益来源，不只是「代码换了个
写法」。`render()` 仍然保留（不删除）：Textual 8.x 的 Widget 基类内部在个别
场景仍会调用 `self.render()`（比如无障碍访问树、初始 `_render()` 缓存），且
一部分既有测试直接调用 `pane.render().plain` 断言画面内容——为避免两条渲染
路径分叉出不一致的行为，`render()` 改成直接复用 `render_line` 的输出拼起来，
不再自己单独维护一份等价逻辑。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from rich.segment import Segment
from rich.text import Text
from textual import events, work
from textual.dom import NoScreen
from textual.geometry import Region
from textual.reactive import reactive
from textual.strip import Strip
from textual.visual import Visual, visualize
from textual.widget import Widget

from pickup import embed


def _row_to_strip(row: list) -> Strip:
    """把一行 `embed.Cell` 编译成 Textual `Strip`：复用 `embed.row_text_and_spans`
    的合并逻辑（跟旧版整屏 `Text` 渲染共用同一份样式合并算法），只是把
    (文本, 样式段) 转成 `Segment` 列表交给 `Strip`——`Strip` 的 cell 宽度按
    Rich 自己的宽字符表自动推算，不需要在这里手动处理 CJK/emoji 占两格。
    """
    text, spans = embed.row_text_and_spans(row)
    if not spans:
        return Strip([Segment(text)] if text else [])
    return Strip([Segment(text[start:end], style) for start, end, style in spans])

# 滚轮一格滚动的行数，与旧版 PREVIEW_MOUSE_SCROLL_LINES 保持一致的手感
WHEEL_SCROLL_LINES = 3
# 有控制通道（事件驱动）时的慢速兜底轮询间隔；没有控制通道时的传统轮询间隔
IDLE_POLL_INTERVAL = 2.0
NO_CHANNEL_POLL_INTERVAL = 0.2
MIN_CAPTURE_INTERVAL = 0.04  # 事件驱动下的最小抓帧间隔，避免 %output 风暴时刷屏
# pane_state 降频查询间隔：光标位置/鼠标标志/回滚量都是慢变状态，但它每次也是
# 一次 tmux fork（约 10ms）。输出风暴期若每帧都查，抓帧循环的 fork 频率直接
# 翻倍（25fps×2，2026-07-19 实测是 pickup 常驻 40%+ CPU 的主要来源）。降到
# 5Hz 缓存复用后光标对 IME 锚定的最多滞后 200ms，无感知；画面文本本身仍按
# %output 事件全速抓帧，输入回显帧率不受影响。
STATE_POLL_INTERVAL = 0.2
# 窗口拖动时 Resize 会连续到达；tmux resize-window + 唤醒抓帧必须防抖，
# 等尺寸停稳再做一次（与 PickupApp 整屏重绘防抖同量级，避免拖动期狂刷）。
_RESIZE_TMUX_DEBOUNCE = 0.12


class EmbedPane(Widget):
    """右栏：托管 tmux 会话的实时画面。

    键盘/粘贴只在本面板持有焦点时转发（需鼠标点到右栏才聚焦）；滚轮按鼠标所在位置
    处理，与焦点无关——列表聚焦时把鼠标移到右栏仍可滚预览或会话历史。

    刻意不设 ALLOW_SELECT=False：保留 Textual 内置的鼠标拖拽选词 + Ctrl+C 复制
    （见 ui/app.py 的 PickupApp 说明）。本类自己只接管滚轮/按键/粘贴/resize，
    不处理 MouseDown/Move/Up，这几类事件会照常落到 Textual 的默认选择逻辑上。
    """

    DEFAULT_CSS = """
    EmbedPane {
        width: 1fr;
        height: 1fr;
        content-align: left top;
    }
    EmbedPane:focus {
        border: none;
    }
    """

    can_focus = True

    session_name: reactive[str | None] = reactive(None)
    dead: reactive[bool] = reactive(False)
    # 不能命名为 scroll_offset：这是 Textual Widget 的二维滚动属性，
    # 覆盖成整数会让框架的文本拖选计算变成 Offset + int。
    history_offset: reactive[int] = reactive(0)
    # 静态对话预览的纵向偏移（行）；与 history_offset 互不共用，避免实时托管状态串扰。
    detail_offset: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        detail_renderer: Callable[[], "Text | str"] | None = None,
        on_focus_list: Callable[[], None] | None = None,
        osc_report: bytes | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._grid: list | None = None
        # 按行缓存的 Strip：render_line(y) 的直接数据源，只有 _sync_strips 判定
        # 某一行真的变了才重新编译那一行，其余行沿用旧对象（见类 docstring）。
        self._strips: list[Strip] | None = None
        # 非实时画面（详情/占位/已结束提示）的 Strip 缓存，键包含决定内容的全部
        # 状态，命中即复用，不是每帧重算——这类画面刷新频率远低于托管会话本身。
        self._static_key: tuple | None = None
        self._static_strips_cache: list[Strip] | None = None
        self._cursor: tuple[int, int, bool] | None = None
        self._detail_renderer = detail_renderer
        self._on_focus_list = on_focus_list
        self._osc_report = osc_report

        # 下面这些字段只由后台抓帧线程写入、主线程（按键/滚轮处理）只读，
        # 避免任何事件处理路径同步调用 embed.capture/embed.pane_state。
        self._mouse_any = False
        self._mouse_sgr = False
        self._history_size = 0

        self._poke = threading.Event()  # 输入/滚动/resize 后立即唤醒抓帧线程补抓一帧
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        # 每次切换展示对象都提升版本。抓帧线程不能只比较 session_name：主线程可能
        # 在它醒来前经历“实时会话 → 详情 → 同一个实时会话”，最终名字虽然没变，
        # 旧帧缓存却已经失效；版本号能让这种快速往返也强制重抓，并拦住旧回调回写。
        self._capture_generation = 0
        self._real_cursor_shown = False  # 外层真实硬件光标当前是否被我们显式打开（见 _set_real_cursor）
        self._resize_tmux_timer = None  # 防抖：拖动停稳后再 resize-window + 抓帧
        self._pending_tmux_size: tuple[int, int] | None = None

    # ---- 生命周期 ----

    def on_mount(self) -> None:
        # 把面板底色设成外层终端真实背景色：托管 Agent 画面里绝大多数格子是"默认
        # 背景"（tmux 报 bg=-1 → Rich bgcolor=None），不显式垫底就会透出 Textual
        # 主题的中性灰，让整个内嵌画面看着变灰。用启动时 OSC 11 探到的真实 RGB
        # 垫底，才能和外层终端底色无缝衔接（老 curses 版是天然透到终端底色的）。
        import pickup

        bg = pickup._background_rgb(self._osc_report)
        if bg is not None:
            self.styles.background = bg
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def on_unmount(self) -> None:
        self._set_real_cursor(False)  # 卸载时收起我们打开的真实光标，别把它漏给退出后的终端
        self._stop.set()
        self._poke.set()
        embed.close_channel()

    # ---- 对外接口：聚焦/切换托管会话 ----

    def focus_session(
        self,
        name: str,
        fallback_renderer: Callable[[], "Text | str"] | None = None,
    ) -> None:
        """把面板切到托管会话；首帧到达前立即展示可用内容或空白终端。"""
        # 后台重扫和重复点击会再次选中同一个会话。已有画面时必须保持幂等，不能
        # 前台清空 _grid、后台却因 tmux 静止帧未变化而跳过解析，永久卡在“连接中…”。
        # 若尚无画面，则提升版本强制后台重新解析，兼顾从旧异常状态自愈。
        reset_capture = self.session_name != name or self._grid is None
        if reset_capture:
            self._capture_generation += 1
            self._grid = None
            self._strips = None
            self._cursor = None
        self.session_name = name
        self._detail_renderer = fallback_renderer
        self.dead = False
        self.detail_offset = 0
        self.invalidate_detail()
        self.history_offset = 0
        channel = embed.open_channel(name, on_output=self._on_pane_output)
        pane_w, pane_h = self._pane_size()
        # 过窄时不 resize：布局尚未稳定或用户把终端缩得很小时，避免 agent
        # 按几列硬换行写进 scrollback（恢复宽度后往上滚仍会看到窄条历史）。
        if embed.should_resize_host(pane_w, pane_h):
            embed.resize(name, pane_w, pane_h)
        # 终端背景色注入：此后 pane 内 agent 的 OSC 11 查询由 tmux 按真实值应答，
        # 深/浅主题自动检测才不会瞎猜（tmux 默认不应答 pane 内的查询）。
        if channel is not None and self._osc_report and embed.supports_theme_report():
            embed.report_theme(channel, self._osc_report)
        self._poke.set()
        return channel  # noqa: RET504（测试里需要断言通道对象，保留返回值）

    def show_detail(self, renderer: Callable[[], "Text | str"] | None) -> None:
        """未托管会话：展示静态详情而非实时画面。"""
        self._capture_generation += 1
        self.session_name = None
        self._grid = None
        self._strips = None
        self._cursor = None
        self._detail_renderer = renderer
        self.dead = False
        self.detail_offset = 0
        self.invalidate_detail()

    def clear(self) -> None:
        self._capture_generation += 1
        self.session_name = None
        self._detail_renderer = None
        self._grid = None
        self._strips = None
        self._cursor = None
        self.dead = False
        self.detail_offset = 0
        embed.close_channel()
        self.invalidate_detail()

    def invalidate_detail(self) -> None:
        """让静态详情/占位缓存失效，并在当前显示静态内容时立即重绘。

        `_detail_renderer` 可能读取会变化的标题缓存、会话状态和摘要；这些变化不
        一定会创建新闭包，因此不能只靠 renderer 身份作为缓存失效条件。主界面
        每次完成列表重建后应调用本方法，使右栏跟同一份最新数据一起刷新。
        """
        self._static_key = None
        self._static_strips_cache = None
        if self.session_name is None or self.dead or self._grid is None:
            self.refresh()

    # ---- 抓帧（后台线程，唯一发起 embed.capture/embed.pane_state 调用的地方）----

    def _pane_size(self) -> tuple[int, int]:
        size = self.content_size
        return max(1, size.width), max(1, size.height)

    def _on_pane_output(self) -> None:
        # 控制通道读线程本来就跑在子线程里，这里只是唤醒抓帧线程，不跨线程动 UI 状态
        self._poke.set()

    def _capture_loop(self) -> None:
        last_frame_key: tuple | None = None
        last_generation = -1
        misses = 0
        last_capture = 0.0
        last_state = None      # pane_state 降频缓存：(光标x, 光标y, 光标可见, mouse_any, mouse_sgr, history_size)
        last_state_at = 0.0
        while not self._stop.is_set():
            try:
                state_refresh_delay: float | None = None
                name = self.session_name
                generation = self._capture_generation
                history_offset = self.history_offset
                if generation != last_generation:
                    last_generation = generation
                    last_frame_key = None
                    misses = 0
                    last_state = None
                    last_state_at = 0.0
                if name is None:
                    self._poke.wait(0.2)
                    self._poke.clear()
                    continue

                channel = embed.active_channel(name)
                gap = time.monotonic() - last_capture
                if 0 < gap < MIN_CAPTURE_INTERVAL:
                    time.sleep(MIN_CAPTURE_INTERVAL - gap)
                pane_w, pane_h = self._pane_size()
                capture_t0 = time.perf_counter()
                text = embed.capture(name, history_offset, pane_h)
                last_capture = time.monotonic()

                if text is None:
                    misses += 1
                    if misses >= 3 and not embed.is_alive(name):
                        self.app.call_from_thread(self._apply_dead, generation, name)
                else:
                    misses = 0
                    now = time.monotonic()
                    polled_state = False
                    if now - last_state_at >= STATE_POLL_INTERVAL:
                        state = embed.pane_state(name)
                        last_state_at = now  # 无论成败都按 5Hz 限速，失败时沿用旧缓存
                        polled_state = True
                        if state is not None:
                            last_state = state
                    state = last_state
                    frame_key = (generation, history_offset, pane_w, pane_h, text)
                    if frame_key != last_frame_key:
                        grid = embed.parse_screen(text, pane_w, pane_h)
                        capture_ms = int((time.perf_counter() - capture_t0) * 1000)
                        if capture_ms >= 100:
                            from pickup import observe
                            observe.event(
                                "capture_slow",
                                duration_ms=capture_ms,
                                session_prefix=(name or "")[:16],
                            )
                        cursor = state[:3] if state is not None else None
                        self.app.call_from_thread(
                            self._apply_capture, generation, name, grid, cursor, state,
                        )
                        # 解析和主线程回写都成功后才提交缓存键；任一步异常都要在
                        # 下一轮重试同一帧，不能又制造一个新的“连接中…”永久中间态。
                        last_frame_key = frame_key
                        if not polled_state:
                            # 输出事件可能在上次状态查询后的 200ms 限速窗口内到达。
                            # 此时画面已是新的、光标却还是旧的；控制通道随后若没有
                            # 更多输出，常规 2s 空闲轮询会让 IME 光标长时间停错位置。
                            # 只补一次恰好落在限速窗口末端的短轮询，既保持 5Hz 上限，
                            # 又保证静止新帧的光标状态及时收敛。
                            state_refresh_delay = max(
                                MIN_CAPTURE_INTERVAL,
                                STATE_POLL_INTERVAL - (now - last_state_at),
                            )
                    elif state is not None:
                        cursor = state[:3]
                        if cursor != self._cursor:
                            self.app.call_from_thread(
                                self._apply_cursor_and_flags, generation, name, cursor, state,
                            )
                        else:
                            self._apply_flags_only(generation, name, state)

                interval = IDLE_POLL_INTERVAL if channel is not None else NO_CHANNEL_POLL_INTERVAL
                if state_refresh_delay is not None:
                    interval = min(interval, state_refresh_delay)
                self._poke.wait(interval)
                self._poke.clear()
            except Exception as exc:  # noqa: BLE001 抓帧失败必须记录并自愈，不能静默杀线程
                import pickup

                pickup._log_embed_error("Textual 抓帧线程", exc)
                last_frame_key = None
                last_state = None
                last_state_at = 0.0
                self._poke.wait(NO_CHANNEL_POLL_INTERVAL)
                self._poke.clear()

    def _capture_is_current(self, generation: int, name: str) -> bool:
        return self._capture_generation == generation and self.session_name == name

    def _apply_capture(self, generation: int, name: str, grid, cursor, state) -> None:
        if not self._capture_is_current(generation, name):
            return
        self._sync_strips(grid)
        self._cursor = cursor
        self.dead = False
        if state is not None:
            self._mouse_any, self._mouse_sgr, self._history_size = state[3], state[4], state[5]
        self._update_app_cursor()

    def _sync_strips(self, grid) -> None:
        """按行 diff 新旧 `embed.Cell` 网格，只重新编译并局部刷新真正变化的行。

        典型场景（敲字、输出滚动）里一帧只有光标附近一两行内容变化，逐行比对
        （`Cell` 是 frozen dataclass，`==` 直接比较字段值，比对本身很便宜）后
        只对变化的行调 `_row_to_strip` 重新编译、只对这些行的 `Region` 调用
        `self.refresh(Region)`——Textual 确认 `render_line` 只会为落在 dirty
        region 内的行被调用，不是「换个 API 名字，内部还是整屏重算」。
        尺寸变化（首帧、resize、回滚窗口高度变化）或还没有旧网格时退化为整屏
        重建，这种情况下逐行比对没有意义。
        """
        old_grid = self._grid
        height = len(grid)
        width = len(grid[0]) if grid else 0
        same_shape = (
            old_grid is not None
            and self._strips is not None
            and len(old_grid) == height
            and (height == 0 or len(old_grid[0]) == width)
        )
        if not same_shape:
            self._strips = [_row_to_strip(row) for row in grid]
            self._grid = grid
            self.refresh()
            return
        strips = self._strips
        regions = []
        for y, (old_row, new_row) in enumerate(zip(old_grid, grid)):
            if old_row != new_row:
                strips[y] = _row_to_strip(new_row)
                regions.append(Region(0, y, width, 1))
        self._grid = grid
        if regions:
            self.refresh(*regions)

    def _apply_cursor_and_flags(self, generation: int, name: str, cursor, state) -> None:
        if not self._capture_is_current(generation, name):
            return
        self._cursor = cursor
        self._mouse_any, self._mouse_sgr, self._history_size = state[3], state[4], state[5]
        self._update_app_cursor()
        self.refresh()

    def _apply_flags_only(self, generation: int, name: str, state) -> None:
        # 纯只读缓存字段更新，不需要跨线程排队：Python 属性赋值本身是原子的
        if not self._capture_is_current(generation, name):
            return
        self._mouse_any, self._mouse_sgr, self._history_size = state[3], state[4], state[5]

    def _apply_dead(self, generation: int, name: str) -> None:
        if not self._capture_is_current(generation, name):
            return
        self.dead = True
        self.invalidate_detail()
        self._update_app_cursor()  # 会话结束后没有可见光标，收起外层真实光标

    # ---- 渲染（Line API：render_line 是真正的绘制入口，见类 docstring）----

    def render_line(self, y: int) -> Strip:
        width = max(1, self.size.width)
        if self.session_name is None or self.dead or self._grid is None:
            strips = self._ensure_static_strips()
            strip = strips[y] if 0 <= y < len(strips) else Strip.blank(width)
        elif self._strips is not None and 0 <= y < len(self._strips):
            strip = self._strips[y]
        else:
            strip = Strip.blank(width)
        # 窗口刚变宽/变窄、新抓帧尚未到达时，缓存行可能仍是旧宽度；按当前面板
        # 宽度裁切或补空白，避免右缘残留旧画面字符（与整屏重绘防抖配合）。
        if strip.cell_length != width:
            strip = strip.adjust_cell_length(width)
        # 自定义 Line API 不会像 Textual 默认 Rich 渲染路径那样自动附加文本
        # 坐标；缺少 offset 元数据时，拖选只能把整个 Widget 识别为全选。
        strip = strip.apply_offsets(0, y)
        return self._apply_selection(strip, y)

    def _apply_selection(self, strip: Strip, y: int) -> Strip:
        """在基础 Strip 上动态叠加当前 Textual 文本选区，不污染行缓存。"""
        try:
            selection = self.text_selection
        except NoScreen:
            # Widget 卸载后仍可能被测试或无障碍读取路径调用 render()；此时已经
            # 没有 Screen，也就不可能存在有效选区，安全返回未叠加高亮的基础行。
            return strip
        if selection is None:
            return strip
        span = selection.get_span(y)
        if span is None:
            return strip
        start, end = span
        cell_length = strip.cell_length
        if end == -1:
            end = cell_length
        start = max(0, min(start, cell_length))
        end = max(start, min(end, cell_length))
        if start == end:
            return strip
        selection_style = self.screen.get_component_rich_style("screen--selection")
        return Strip.join((
            strip.crop(0, start),
            strip.crop(start, end).apply_style(selection_style),
            strip.crop(end),
        ))

    def selection_updated(self, selection) -> None:
        # 基础缓存刻意不含选区样式；选区变化只需重绘，render_line 会按最新
        # text_selection 动态叠加高亮，不需要重编译整屏 Cell/Visual。
        self.refresh()

    def _static_renderable(self) -> "Text":
        """未托管/已结束/尚无首帧这几种非实时状态下要展示的内容，逻辑与旧版
        `render()` 完全一致（只是从"每次渲染都现算"搬到"状态变化时算一次"）。
        """
        from pickup.i18n import t

        if self.dead:
            return Text(t("detail.session_ended"))
        if self._detail_renderer is not None:
            rendered = self._detail_renderer()
            return rendered if isinstance(rendered, Text) else Text(str(rendered))
        if self.session_name is None:
            return Text(t("detail.pick_session"))
        # 会话已聚焦但还没有第一帧：连接/抓帧是后台实现细节，不能暴露成用户
        # 可见的中间态，展示空白终端画布，首帧到达后无缝替换。
        return Text()

    def _is_detail_view(self) -> bool:
        """右栏正在展示静态对话预览（非实时托管画面）。"""
        return self.session_name is None and self._detail_renderer is not None and not self.dead

    def _detail_full_strips(self) -> list[Strip]:
        """把详情全文编译成完整 Strip 列表（高度不限），供窗口切片与滚动上限计算。"""
        visual = visualize(self, self._static_renderable())
        return Visual.to_strips(
            self, visual, self.size.width, None, self.visual_style,
            apply_selection=False,
        )

    def _detail_max_offset(self, full_len: int | None = None) -> int:
        pane_h = max(1, self.size.height)
        if full_len is None:
            full_len = len(self._detail_full_strips())
        return max(0, full_len - pane_h)

    def scroll_detail(self, delta: int) -> bool:
        """滚动静态对话预览；返回是否处于可滚动的详情态（不论偏移是否真的变了）。"""
        if not self._is_detail_view():
            return False
        max_off = self._detail_max_offset()
        new_offset = max(0, min(self.detail_offset + delta, max_off))
        if new_offset != self.detail_offset:
            self.detail_offset = new_offset
            self._static_key = None
            self._static_strips_cache = None
            self.refresh()
        return True

    def scroll_detail_home(self) -> bool:
        if not self._is_detail_view():
            return False
        if self.detail_offset != 0:
            self.detail_offset = 0
            self._static_key = None
            self._static_strips_cache = None
            self.refresh()
        return True

    def scroll_detail_end(self) -> bool:
        if not self._is_detail_view():
            return False
        max_off = self._detail_max_offset()
        if self.detail_offset != max_off:
            self.detail_offset = max_off
            self._static_key = None
            self._static_strips_cache = None
            self.refresh()
        return True

    def scroll_detail_page(self, direction: int) -> bool:
        """direction: -1 上翻一页，+1 下翻一页。"""
        pane_h = max(1, self.size.height)
        return self.scroll_detail(direction * max(1, pane_h - 1))

    def _ensure_static_strips(self) -> list[Strip]:
        """把 `_static_renderable()` 编译成整屏 Strip 列表并按状态缓存。

        这类画面（详情/占位/已结束提示）刷新频率远低于托管会话实时画面，不需要
        像 `_sync_strips` 那样按行 diff；但 `render_line` 每次重绘要为每一可见行
        各调用一次，若不缓存就要对同一份内容反复重新换行——缓存键覆盖全部
        决定内容的稳定状态（会话名、是否已结束、详情渲染器身份、当前尺寸、
        预览滚动偏移），命中即复用。renderer 捕获的标题缓存、状态或摘要发生
        变化时，调用方必须调用公开的 `invalidate_detail()`；`show_detail()`/
        `focus_session()`/`clear()`/resize 已在本类内部自动失效。
        转换本身复用 Textual 内置的 `Visual.to_strips`（与 Widget 基类默认的
        Rich-renderable 渲染路径同一套换行/对齐/宽字符处理），不用自己重新
        实现文本折行和 `content-align: left top` 这条 CSS 的效果。

        静态对话预览：先按无高度上限整篇排版，再按 `detail_offset` 切一屏窗口；
        这样长对话不会被裁掉，键盘/滚轮可以上下翻看。
        """
        key = (
            self.session_name, self.dead, id(self._detail_renderer),
            self.size, self.detail_offset,
        )
        if self._static_key == key and self._static_strips_cache is not None:
            return self._static_strips_cache
        pane_h = max(1, self.size.height)
        width = max(1, self.size.width)
        if self._is_detail_view():
            full = self._detail_full_strips()
            max_off = max(0, len(full) - pane_h)
            offset = max(0, min(self.detail_offset, max_off))
            window = list(full[offset:offset + pane_h])
            while len(window) < pane_h:
                window.append(Strip.blank(width))
            strips = window
        else:
            visual = visualize(self, self._static_renderable())
            strips = Visual.to_strips(
                self, visual, width, pane_h, self.visual_style,
                apply_selection=False,
            )
        self._static_key = key
        self._static_strips_cache = strips
        return strips

    def render(self) -> "Text":
        """兼容方法：生产渲染已经完全走 `render_line`（Textual 8.x 的 Widget
        基类在个别内部路径——比如首次 `_render()` 缓存、无障碍访问树——仍可能
        调用 `self.render()`，且既有测试直接调用 `pane.render().plain` 断言
        画面内容），保留它但不再自己维护一份等价逻辑，保证两条路径不会分叉。

        注意这里刻意**不做** `Visual.to_strips` 的换行/对齐/全屏 padding——这些
        效果是 `render_line` 路径本身（经 `_ensure_static_strips`）已经处理的，
        若 `render()` 也做一遍，返回的就是整屏带空白的对齐布局，而不是调用方
        期待的"原始文本内容"。一组既有测试（`test_first_frame_never_exposes_...`、
        `test_stale_capture_callback_cannot_overwrite_new_view` 等）直接断言
        `pane.render().plain == "即时会话详情"`（**纯原始文本，不含任何对齐
        空白**），这些测试是项目文档明确固化的行为（「内部状态驱动渲染结果」
        的断言标准），不能为了迎合新的实现去改测试。所以：
        - 非实时状态（详情/占位/已结束/尚无首帧）：直接返回 `_static_renderable()`
          的原始 `Text` 内容（不对齐填充、不换行）——这跟旧版 `render()` 在这些
          分支里 `return rendered if isinstance(rendered, Text) else Text(...)`
          的行为字节级一致。
        - 实时画面：拼接 `render_line` 的 `.text`（`Strip.text` 只含字符内容、
          不含样式），生成跟旧版 `render()` 相同的纯文本画面——旧版也是用
          `embed.grid_to_text(self._grid)` 逐行拼字符，样式部分从来不影响
          `.plain`，所以这条路径同样保持原有契约。
        """
        if self.session_name is None or self.dead or self._grid is None:
            return self._static_renderable()
        height = max(1, self.size.height or 1)
        return Text("\n".join(self.render_line(y).text for y in range(height)))

    def get_content_width(self, container, viewport) -> int:
        return max(1, container.width)

    def get_content_height(self, container, viewport, width) -> int:
        return max(1, container.height)

    # ---- 光标锚定：IME 候选框需要终端硬件光标停在 pane 内的真实位置 ----
    #
    # Textual 用 App.cursor_position（屏幕绝对坐标）控制"真实终端硬件光标"该停
    # 在哪——这是 Textual 官方文档里专门给 IME/emoji 弹出框定位用的接口
    # （Input/TextArea 等内置控件在光标移动时都会写这个属性）。旧版 curses 用
    # `stdscr.move(cy, pane_x0 + cx)` 做的事，这里是它在 Textual 下的对应写法；
    # 不写这个属性时，终端光标停在 Textual 默认位置，中文输入法候选词会跟着
    # 那个默认位置走，而不是 pane 内实际光标处（真机实测过的问题）。

    def _update_app_cursor(self) -> None:
        offset = self._cursor_local_offset() if self.has_focus else None
        if offset is None:
            # 失焦、会话已结束或托管程序自己藏了光标：把外层真实光标也藏回去，
            # 恢复 Textual 全屏应用的默认（无可见硬件光标）状态。
            self._set_real_cursor(False)
            return
        self.app.cursor_position = self.region.offset + offset
        # 显示外层真实硬件光标并停在 pane 内光标处：Textual 全屏运行期默认把真实
        # 光标 `\e[?25l` 藏掉（只在退出时才恢复），只靠 App.cursor_position 移动一个
        # 看不见的光标。但 macOS 等系统的输入法（IME）靠"可见的真实光标"来决定候选
        # 词窗口位置、甚至据此决定是否激活中文合成——真实光标被藏起来时，用户在
        # 内嵌 Agent 里根本打不出中文（真机反馈）。这里显式 `\e[?25h` 打开真实光标，
        # 效果等同 tmux/screen attach 时把外层光标停在活动 pane 的光标处——那正是
        # 嵌套终端里 IME 能正常工作的原因。
        self._set_real_cursor(True)

    def _set_real_cursor(self, visible: bool) -> None:
        """显式开关外层终端的真实硬件光标（DECTCEM，`\\e[?25h`/`\\e[?25l`）。

        Textual 每帧只负责把（可能隐藏的）光标移到 App.cursor_position，不会主动
        重新隐藏，所以这里写一次就会保持，直到我们反向写回。去重避免每帧重复写。
        """
        if visible == self._real_cursor_shown:
            return
        driver = getattr(self.app, "_driver", None)
        if driver is None:
            return
        try:
            driver.write("\x1b[?25h" if visible else "\x1b[?25l")
            driver.flush()
        except Exception:  # noqa: BLE001 光标显隐失败不该影响输入转发主流程
            return
        self._real_cursor_shown = visible

    def watch_has_focus(self, has_focus: bool) -> None:
        # Textual 派发 Focus/Blur 时 reactive `has_focus` 往往尚未翻转；在事件处理
        # 里读 self.has_focus 会得到旧值，导致「已聚焦却按失焦路径藏光标」。跟
        # reactive 同步后再刷新外层真实光标，IME 锚定才稳定。
        self._update_app_cursor()

    def _cursor_local_offset(self):
        """内嵌 pane 光标相对本 widget 内容区的偏移；None 表示当前不该显示光标。"""
        if self.session_name is None or self.dead or self._cursor is None:
            return None
        cx, cy, visible = self._cursor
        if not visible:
            return None
        pane_w, pane_h = self._pane_size()
        cx = max(0, min(cx, max(0, pane_w - 1)))
        cy = max(0, min(cy, max(0, pane_h - 1)))
        from textual.geometry import Offset
        return Offset(cx, cy)

    def _on_focus(self, event: events.Focus) -> None:
        # 保留事件钩子；真正可靠的刷新在 watch_has_focus。
        self._update_app_cursor()

    def _on_blur(self, event: events.Blur) -> None:
        # 焦点离开 pane（切回列表 / 打开弹窗）时收起真实光标，交还给 Textual 默认态。
        self._update_app_cursor()

    # ---- 输入转发 ----

    async def _on_key(self, event: events.Key) -> None:
        if self._is_detail_view():
            # 面板聚焦时方向键/翻页也滚预览（列表聚焦时由 MainScreen 优先级绑定处理）
            handled = False
            if event.key == "up":
                handled = self.scroll_detail(-1)
            elif event.key == "down":
                handled = self.scroll_detail(1)
            elif event.key == "pageup":
                handled = self.scroll_detail_page(-1)
            elif event.key == "pagedown":
                handled = self.scroll_detail_page(1)
            elif event.key == "home":
                handled = self.scroll_detail_home()
            elif event.key == "end":
                handled = self.scroll_detail_end()
            if handled:
                event.stop()
                event.prevent_default()
            return
        name = self.session_name
        if not name or self.dead:
            return
        if event.key == "ctrl+backslash":
            # 焦点回列表：托管会话在后台 tmux 继续跑，滚动位置保留，回车/下次
            # 聚焦时接着看。旧版靠"双击反斜杠 300ms 窗口"消歧义是 curses raw
            # 模式下没法区分 Ctrl+\ 与连按两次 \ 的权宜之计；Textual 的按键
            # 解析器本就把两者识别成不同的 key（"ctrl+backslash" vs "\\"），
            # 这里不再需要那套时间窗口 hack。
            event.stop()
            if self._on_focus_list is not None:
                self._on_focus_list()
            return
        if event.key == "ctrl+c":
            # 真机排查记录：Textual 的按键派发是"事件先转发到当前聚焦 widget
            # 的 on_key，widget 自己处理并 stop() 掉之后，BINDINGS（包括本类
            # 曾经定义的 ctrl+c 绑定）根本没有机会介入"——不是"widget 绑定优先
            # 于 Screen 绑定"那套（用 Pilot 实测验证过，之前基于 BINDINGS 的
            # 实现是死代码，从未被真正调用到）。选择/复制逻辑必须直接写在这里。
            event.stop()
            event.prevent_default()
            selected = self.screen.get_selected_text()
            if selected:
                self.app.copy_to_clipboard(selected)
            else:
                embed.send_key(name, "C-c")
            return
        event.stop()
        event.prevent_default()
        if self.history_offset > 0 and event.key in ("up", "down"):
            # 回滚状态下方向键先退回直播画面，和旧版「按键直接发往会话」一致，
            # 但滚轮之外的操作应先让用户看清直播画面再决定是否继续操作
            self.history_offset = 0
            self._poke.set()
            return
        if event.is_printable and event.character:
            embed.send_literal(name, event.character)
            return
        translated = embed.translate_textual_key(event.key)
        if translated is not None:
            embed.send_key(name, translated[1])

    def _on_paste(self, event: events.Paste) -> None:
        if not self.session_name or self.dead:
            return
        # 浏览器增强脚本（shell-gate 注入进 ttyd 页面）把剪贴板图片压缩后裹上
        # 哨兵标记，经普通粘贴通道送到这里；识别到就落盘再把路径喂给 agent，
        # 不当文本转发。普通文本粘贴走原有路径不受影响。
        image_bytes = embed.extract_pasted_image(event.text)
        if image_bytes is not None:
            self._paste_image_worker(self.session_name, image_bytes)
        else:
            embed.paste(self.session_name, event.text)
        event.stop()

    @work(thread=True, group="paste-image")
    def _paste_image_worker(self, name: str, image_bytes: bytes) -> None:
        # 查 pane 工作目录 + 落盘 + paste() 都要走 tmux 子进程/控制通道，
        # 不能在主线程做（同类约束见 _host_and_focus 等其它 tmux 调用）。
        path = embed.save_image_and_paste_path(name, image_bytes)
        if path is None:
            self.app.bell()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(65, event.x, event.y, -WHEEL_SCROLL_LINES)
        event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(64, event.x, event.y, WHEEL_SCROLL_LINES)
        event.stop()

    def _wheel(self, sgr_button: int, x: float, y: float, local_delta: int) -> None:
        """滚轮事件：静态对话预览直接滚 `detail_offset`；托管会话则按鼠标上报/
        tmux 历史回滚处理（见函数体）。

        转发只发 press 序列（xterm 规范：滚轮 64/65 没有 release 事件），且经
        `embed.send_mouse_sequence` 排队到后台线程发送——触控板惯性滚动一秒能
        产生上百个事件，若在主线程同步 fork tmux（每个约 10ms）会把整个界面
        堵死（2026-07-19 真机定位的卡顿根因）。
        `mouse_any`/`mouse_sgr` 读的是后台抓帧线程缓存的字段，这里绝不发起新的
        embed.pane_state 阻塞调用。
        """
        if self._is_detail_view():
            # 文档式预览：detail_offset 0=顶部、增大=更靠后的对话。
            # 传入的 local_delta 沿用 history_offset 约定（Up=+、Down=-，0=直播底），
            # 与文档滚动符号相反，这里取反才能「下滚看更晚内容」。
            self.scroll_detail(-local_delta)
            return
        name = self.session_name
        if not name or self.dead:
            return
        if self._mouse_any:
            col, row = int(x) + 1, int(y) + 1  # tmux SGR 坐标 1-based
            embed.send_mouse_sequence(name, embed.sgr_mouse_sequence(sgr_button, col, row))
            return
        self._scroll(local_delta)

    def _scroll(self, delta: int) -> None:
        name = self.session_name
        if not name or self.dead:
            return
        new_offset = max(0, min(self.history_offset + delta, self._history_size))
        if new_offset == self.history_offset:
            return
        self.history_offset = new_offset
        self._poke.set()  # 唤醒后台线程按新 offset 立即补抓一帧，主线程不等待

    def _on_resize(self, event: events.Resize) -> None:
        # 静态详情按尺寸缓存；尺寸变了必须失效。实时画面行宽由 render_line 按当前
        # 面板宽度裁补。tmux resize-window + 抓帧走防抖：拖动过程中只记目标尺寸，
        # 停稳后再改托管窗并补抓，避免连续 Resize 狂刷 tmux。
        self.invalidate_detail()
        self._update_app_cursor()
        # 不在这里 self.refresh()：Textual 布局阶段的 _size_updated 已把本控件标脏；
        # 拖动期多余 refresh 只会加重局部重绘。昂贵的 tmux 路径走下面的防抖。
        name = self.session_name
        if not name or self.dead:
            return
        self._pending_tmux_size = (
            max(1, event.size.width),
            max(1, event.size.height),
        )
        if self._resize_tmux_timer is not None:
            self._resize_tmux_timer.stop()
        self._resize_tmux_timer = self.set_timer(
            _RESIZE_TMUX_DEBOUNCE,
            self._apply_pending_tmux_resize,
        )

    def _apply_pending_tmux_resize(self) -> None:
        """防抖到期：把托管会话窗口调到最后一次目标尺寸并唤醒抓帧。"""
        self._resize_tmux_timer = None
        size = self._pending_tmux_size
        self._pending_tmux_size = None
        name = self.session_name
        if not name or self.dead or size is None:
            return
        if not embed.should_resize_host(size[0], size[1]):
            return
        embed.resize(name, size[0], size[1])
        self._poke.set()
