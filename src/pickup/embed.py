"""内嵌宿主：把托管在 tmux 里的会话画面搬进 pickup 的 Textual 右栏。

与 keepalive.py 平级的运行时无关层：keepalive 管「把启动计划包进 tmux 以便
SSH 断线保活」，本模块管「不 attach——用 capture-pane 拿画面、send-keys 送按键」，
让右栏展示会话现场，会话在后台 tmux 里持续运行，随时经列表切换。与保活共用
`tmux -L pickup-keepalive` socket 和 pickup-* 命名空间：keepalive.annotate()
状态标注、reap_idle() 空闲回收对内嵌会话全部照旧生效。键盘焦点由界面层管理
（默认侧边栏，点右栏才交互）；本模块不感知焦点。
适配器不感知本模块；主要调用方是 `ui.embed_pane.EmbedPane`。

渲染保真度由 tmux 自己保证（它就是终端模拟器）：本模块只解析 capture-pane -e
输出的 SGR 颜色序列，不做完整 VT100 模拟。
"""

from __future__ import annotations

import base64
import binascii
import collections
import functools
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from rich.cells import cell_len as _rich_cell_len
from rich.color import Color
from rich.style import Style

from pickup import keepalive
from pickup.models import LaunchPlan

_CREATE_TIMEOUT = 5.0
_CALL_TIMEOUT = 1.5

_PASTE_BUFFER = "pickup-embed"


class EmbedError(Exception):
    """内嵌宿主操作失败（创建会话、写入按键等）；界面层据此 beep 或提示。"""


def available(disabled_flag: bool = False) -> bool:
    """内嵌可用性：tmux 已安装且未被显式禁用（--no-keepalive / PICKUP_KEEPALIVE=0，
    旧变量名 SC_KEEPALIVE=0 同样生效）。

    与 keepalive.enabled() 不同，不检查 TMUX/STY：内嵌从不 attach，只读写
    独立 socket 上的会话，用户自己的 tmux/screen 嵌套一层没有副作用。
    """
    if disabled_flag:
        return False
    if keepalive._env_disabled("PICKUP_KEEPALIVE", "SC_KEEPALIVE"):
        return False
    return shutil.which("tmux") is not None


def host_session(
    plan: LaunchPlan, runtime_id: str, ident: str, width: int, height: int,
    osc_report: bytes | None = None,
) -> str:
    """把启动计划以 detached 方式托管进保活 socket，返回 tmux 会话名。

    与 keepalive.wrap_plan 同命名空间、同环境变量注入；区别在不 attach（-d），
    并按面板实际尺寸创建（-x/-y），避免先 80x24 再 resize 的重排闪烁。
    会话名已存在（极端情况下同名残留）时视为复用，直接返回名字。
    创建时经 -P 顺带取回 pane_id 记入 _pane_ids——refresh -r 背景色注入需要
    pane_id 寻址，创建时就拿可以省掉一次 display-message 往返。

    `osc_report` 非空且 tmux 支持 refresh-client -r 时，创建成功后立即（用一个
    专属的、会保持连接的控制通道）注入背景色，而不是等调用方后续聚焦面板时
    才注入——尽量把窗口提前。

    真机排查记录（这条限制目前无法从 pickup 这一侧完全消除，如实记录）：
    refresh-client -r 依赖"当前有一个控制模式客户端连接着"这个前提——一旦
    注入用的通道关闭，效果会跟着消失（实测：一次性开关道注入后立刻关闭，
    托管进程此后一次都查不到颜色，比完全不做早注入还差）；且它只影响"pane
    尚未被回答过"的后续查询，一旦某次查询已经被 tmux 用默认猜测值（通常是
    纯黑）答复过，那次查询的结果就定死了，之后再注入也不能让已经用掉错误
    答案的进程回头重新查一遍。也就是说，创建会话到真实 agent 自己发起第一次
    查询之间，仍然存在无法从时序上完全消灭的竞态窗口——这是 tmux 控制协议本
    身的限制，不是可以单靠调整 pickup 这边调用顺序解决的。
    """
    name = keepalive._session_name(runtime_id, ident)
    argv = [
        *keepalive._BASE_ARGV, "-f", keepalive._ensure_config_file(),
        "new-session", "-d", "-P", "-F", "#{pane_id}",
        "-s", name, "-x", str(width), "-y", str(height),
    ]
    if plan.cwd:
        argv += ["-c", plan.cwd]
    argv += [
        "-e", f"PICKUP_RUNTIME={runtime_id}",
        "-e", f"PICKUP_SESSION_ID={ident}",
        # 旧变量名继续注入，与 keepalive.wrap_plan 保持一致
        "-e", f"SC_RUNTIME={runtime_id}",
        "-e", f"SC_SESSION_ID={ident}",
        "--",
        *plan.argv,
    ]
    try:
        proc = subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=_CREATE_TIMEOUT)
        pane_id = (proc.stdout or b"").decode().strip()
        if pane_id.startswith("%"):
            _pane_ids[name] = pane_id
    except subprocess.CalledProcessError as exc:
        if is_alive(name):
            return name  # 同名会话已在跑：复用而不是报错
        raise EmbedError(f"无法创建内嵌会话 {name}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EmbedError(f"无法创建内嵌会话 {name}：{exc}") from exc
    if osc_report and supports_theme_report():
        open_channel(name)  # 让 report_theme 依赖的"当前有客户端连接"前提尽早成立
        channel = active_channel(name)
        if channel is not None:
            report_theme(channel, osc_report)
    return name


# 会话名 → pane_id（host_session 创建时经 new-session -P 取回）；同名复用等
# 未登记路径由 ControlChannel._query_pane_id 外部查询兜底
_pane_ids: dict[str, str] = {}


def is_alive(name: str) -> bool:
    """托管会话是否还活着（pane 里的进程退出后 tmux 会话随之消失）。"""
    if shutil.which("tmux") is None:
        return False
    try:
        subprocess.run(
            [*keepalive._BASE_ARGV, "has-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def capture(name: str, scroll_offset: int = 0, pane_height: int = 0) -> str | None:
    """抓取会话画面（带 SGR 颜色序列）；会话不存在或抓取失败返回 None。

    scroll_offset > 0 时抓「从 live 窗口向上回滚 offset 行」的历史窗口——
    应用层滚动必须用这种方式：copy-mode 的滚动偏移只作用于 client 渲染层，
    capture-pane 抓的 pane buffer 永远停在 live 窗口（实测 scroll_position 变化
    对 capture 内容零影响，这是内嵌滚轮最初不可见的根因）。

    控制通道存活时优先走 `ControlChannel.request`（消灭抓帧循环每帧一次 fork，
    这是内嵌面板 CPU 占用的主要来源之一）；通道缺失/请求失败时回退外部 fork——
    只读查询走通道本就安全（见 ControlChannel 类注释的纪律），失败原因可能是
    会话真的没了，交给下面 fork 路径的 subprocess 异常分类统一处理更可靠。
    """
    args = ["capture-pane", "-p", "-e"]
    if scroll_offset > 0 and pane_height > 0:
        # 实测钉死的窗口公式：-S -offset -E (h-1-offset) = live 窗口精确上移 offset 行
        # （seq 1 100 会话里 -S -6 -E 13 得 76..95，相对 live 82..101 正好上移 6）
        args += ["-S", f"-{scroll_offset}", "-E", str(pane_height - 1 - scroll_offset)]
    args += ["-t", name]
    ch = _active_channel(name)
    if ch is not None:
        lines = ch.request(*args)
        if lines is not None:
            return "\n".join(lines)
    try:
        out = subprocess.check_output([*keepalive._BASE_ARGV, *args],
                                      stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.decode("utf-8", errors="replace")


def pane_state(name: str) -> tuple[int, int, bool, bool, bool, int] | None:
    """一次查询拿全 pane 交互状态：(光标 x, 光标 y, 光标可见, 程序申请了鼠标,
    SGR 鼠标模式, 回滚行数 history_size)。

    合并进单个 display-message 调用——capture 循环每轮都要光标位置，滚轮转发要鼠标
    模式，应用层滚动的上限判定要回滚量，分开查每轮就是三次 fork。查询失败返回 None。
    控制通道优先，回退路径同 capture()。
    """
    args = ["display-message", "-p", "-t", name,
            "#{cursor_x}|#{cursor_y}|#{cursor_flag}|#{mouse_any_flag}|#{mouse_sgr_flag}"
            "|#{history_size}"]
    ch = _active_channel(name)
    out: str | None = None
    if ch is not None:
        lines = ch.request(*args)
        if lines is not None:
            out = "\n".join(lines).strip()
    if out is None:
        try:
            out = subprocess.check_output(
                [*keepalive._BASE_ARGV, *args],
                stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT,
            ).decode().strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
    try:
        xs, ys, fs, ma, ms, hs = out.split("|")
        return int(xs), int(ys), fs == "1", ma == "1", ms == "1", int(hs)
    except ValueError:
        return None


# 托管窗过窄时，Cursor/Claude 等会按当前列数硬换行并写入 scrollback；
# 之后哪怕右栏恢复正常宽度，往上滚仍会看到「只剩几列」的历史。创建时抬到下限；
# 后续缩放若仍低于下限则跳过，保留上一次可用尺寸。
MIN_HOST_WIDTH = 40
MIN_HOST_HEIGHT = 10


def normalize_host_size(width: int, height: int) -> tuple[int, int]:
    """创建托管会话用：宽高至少到下限，避免首帧就按极窄几何排版。"""
    return max(MIN_HOST_WIDTH, max(1, int(width))), max(MIN_HOST_HEIGHT, max(1, int(height)))


def should_resize_host(width: int, height: int) -> bool:
    """后续 resize-window：低于下限则不要改托管窗。"""
    return int(width) >= MIN_HOST_WIDTH and int(height) >= MIN_HOST_HEIGHT


def resize(name: str, width: int, height: int) -> None:
    """把托管会话的窗口调整为面板尺寸；失败静默（会话可能刚好退出）。

    调用方应先用 `should_resize_host` 过滤过窄尺寸；此处再兜底一次，
    防止遗漏路径把几列宽烧进历史。
    """
    if not should_resize_host(width, height):
        return
    ch = _active_channel(name)
    if ch is not None and ch.command(
            "resizew", "-t", name, "-x", str(width), "-y", str(height)):
        return
    try:
        subprocess.run(
            [*keepalive._BASE_ARGV, "resize-window", "-t", name,
             "-x", str(width), "-y", str(height)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def send_literal(name: str, text: str, *, force_fork: bool = False) -> None:
    """按字面量发送文本（不做键名解释），用于可打印字符的批量输入。

    控制通道存活时走通道（消灭每键一次 fork）；force_fork 强制走外部子进程——
    SGR 鼠标序列这类转义密集的内容按 tmuxy 的实测经验走外部更稳。
    """
    if not text:
        return
    ch = None if force_fork else _active_channel(name)
    if ch is not None and ch.command("send", "-l", "-t", name, "--", text):
        return
    _send(name, ["send-keys", "-l", "-t", name, "--", text])


def send_key(name: str, *keys: str) -> None:
    """按 tmux 键名发送特殊键（Enter / C-c / Up / BSpace …）。"""
    if not keys:
        return
    ch = _active_channel(name)
    if ch is not None and ch.command("send", "-t", name, "--", *keys):
        return
    _send(name, ["send-keys", "-t", name, "--", *keys])


def _send(name: str, argv: list[str]) -> None:
    try:
        subprocess.run(
            [*keepalive._BASE_ARGV, *argv],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # 会话刚好退出时丢键静默，画面会经 capture 轮询收敛到"已结束"


def paste(name: str, text: str) -> None:
    """整段粘贴：经 paste buffer 一次性注入，-p 让目标程序按 bracketed paste 接收。

    刻意走外部子进程而不走控制通道：多行文本含换行，无法作为控制模式行协议的
    单条命令参数；且数据注入类命令（与 send-keys -l 同类）外部执行是安全的。
    """
    if not text:
        return
    try:
        subprocess.run(
            [*keepalive._BASE_ARGV, "set-buffer", "-b", _PASTE_BUFFER, "--", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
        subprocess.run(
            [*keepalive._BASE_ARGV, "paste-buffer", "-p", "-d", "-b", _PASTE_BUFFER, "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# ---------------------------------------------------------------------------
# 剪贴板图片粘贴：经浏览器增强脚本（shell-gate 注入进 ttyd 页面）压缩编码后，
# 以 term.paste() 送成一次普通粘贴，用不易冲突的哨兵标记裹住 base64 图片数据。
# 与文本粘贴共用 `_on_paste` 入口，靠哨兵前后缀区分，不新开传输通道。
# ---------------------------------------------------------------------------

_IMG_SENTINEL_BEGIN = "␞PICKUP_IMG_BEGIN␞"
_IMG_SENTINEL_END = "␞PICKUP_IMG_END␞"
_IMAGE_PASTE_DIR = ".pickup-pastes"


def extract_pasted_image(text: str) -> bytes | None:
    """识别浏览器增强脚本裹了哨兵的图片粘贴内容，解出原始图片字节；

    普通文本粘贴（不含哨兵）返回 None，调用方应转发原文本，走现有 `paste()`。
    """
    if not (text.startswith(_IMG_SENTINEL_BEGIN) and text.endswith(_IMG_SENTINEL_END)):
        return None
    payload = text[len(_IMG_SENTINEL_BEGIN):-len(_IMG_SENTINEL_END)]
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None


def _pane_cwd(name: str) -> str | None:
    """查询托管 pane 当前工作目录；查询失败返回 None，调用方退化到系统临时目录。"""
    args = ["display-message", "-p", "-t", name, "#{pane_current_path}"]
    ch = _active_channel(name)
    if ch is not None:
        lines = ch.request(*args)
        if lines:
            out = "\n".join(lines).strip()
            if out:
                return out
    try:
        out = subprocess.check_output(
            [*keepalive._BASE_ARGV, *args],
            stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT,
        ).decode().strip()
        return out or None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def save_image_and_paste_path(name: str, image_bytes: bytes) -> str | None:
    """把粘贴来的图片落盘为临时文件，再把绝对路径经 `paste()` 送进托管 pane，
    让 Claude Code / Codex 等按路径读图。

    图片优先落在托管 pane 当前工作目录下的隐藏子目录（路径短、agent 大概率有
    权限访问）；查不到工作目录时退化到系统临时目录。落盘失败返回 None，调用方
    据此提示用户重试；不在这里改动运行时适配器，保持本模块运行时无关。
    """
    cwd = _pane_cwd(name)
    base_dir = (
        os.path.join(cwd, _IMAGE_PASTE_DIR) if cwd
        else os.path.join(tempfile.gettempdir(), "pickup-pastes")
    )
    try:
        os.makedirs(base_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="paste-", suffix=".jpg", dir=base_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
    except OSError:
        return None
    paste(name, path)
    return path


# ---------------------------------------------------------------------------
# 控制模式通道：常驻 `tmux -C attach` 子进程，命令写 stdin、事件读 stdout
#
# 收益：send-keys 等修改类命令不再每键 fork 一个 tmux 进程（写管道 <1ms）；
# %output 事件让 capture 循环从 200ms 盲轮询变为事件驱动（回显延迟的来源消除）。
#
# 纪律（tmuxy 项目对 3.3a/3.5a 的实测记录）：控制 client attach 期间，外部子进程
# 并发执行「修改会话状态」的命令可能 crash 服务端，所以 send-keys/copy-mode/
# resize-window/refresh -r 在通道存活时必须全部走通道；capture-pane、
# display-message 等只读查询以及 send-keys -l 注入 SGR 鼠标序列（转义密集内容）
# 走外部子进程仍然安全。通道死亡时所有调用自动回退到外部 fork 路径。
# ---------------------------------------------------------------------------

_CTL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./:%@=+,^")


def _ctl_quote(arg: str) -> str:
    """控制模式命令行参数转义：干净 ASCII 原样通过，其余双引号包裹并转义 \\ " $ `。

    参数不得含换行（行协议按行分隔命令）；ESC/BEL 等控制字节在双引号内字面有效
    （refresh -r 的 OSC 应答序列就靠这个透传）。
    """
    if arg and all(c in _CTL_SAFE for c in arg):
        return arg
    return '"' + (arg.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("$", "\\$").replace("`", "\\`")) + '"'


# 匹配 %begin/%end/%error 守卫行，提取 (时间戳, 命令号)——tmux(1) 控制模式协议：
# 每条命令产生一个 %begin ts num [flags] ... %end/%error ts num [flags] 块，
# 同一块的 begin/end 共享相同的 (ts, num)。只用前两个数字字段做匹配，不管
# 后面可能存在的 flags，兼容不同 tmux 版本的尾部格式差异。
_GUARD_RE = re.compile(r"^%(begin|end|error)\s+(\d+)\s+(\d+)")
_STARTUP_WAITER = object()
_ASYNC_NOTIFICATIONS = frozenset({
    "%client-detached", "%client-session-changed", "%client-window-changed",
    "%extended-output", "%layout-change", "%message", "%output",
    "%paste-buffer-changed", "%paste-buffer-deleted", "%pause",
    "%session-changed", "%session-renamed", "%session-window-changed",
    "%sessions-changed", "%subscription-changed", "%unlinked-window-add",
    "%unlinked-window-close", "%unlinked-window-renamed", "%window-add",
    "%window-close", "%window-pane-changed", "%window-renamed",
})


class ControlChannel:
    """到某个托管会话的控制模式通道。

    stdin 写命令（tmux 命令行语法，参数经 _ctl_quote）；stdout 由读线程按行协议
    消费：%output → on_output 回调（capture 循环的抓帧信号）、%pause → 自动回
    refresh -A 恢复（tmux 3.2+ pause-after 流控）、%exit/EOF → 标记死亡、
    %begin…%end/%error → 同步命令的响应块（见 request()）。
    """

    def __init__(self, name: str, on_output=None) -> None:
        self.name = name
        self.on_output = on_output
        self.dead = False
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._ready = threading.Event()
        self._startup_ok = False
        # FIFO：每次成功写入一条命令就 append 一个「响应接收方」占位（None 表示
        # 调用方不关心响应，即现有 command()/send() 的火后不理用法；否则是
        # request() 传入的 maxsize=1 Queue）。tmux 控制模式命令响应严格按发送
        # 顺序返回、块与块之间不交叉（协议保证），因此不管 request()/command()
        # 分别在哪个线程调用，只要写入 stdin 与登记这个占位在同一把锁内原子
        # 完成，_read_loop 按到达顺序 popleft 消费就必然对应正确的调用方——
        # 不依赖任何按时间戳/命令号做跨线程匹配的假设。
        # 第一项预留给 `tmux -C attach` 自身的启动响应。必须等这块响应完整消费后
        # 才能允许业务命令进入 FIFO，否则首条业务 waiter 会被 attach 的 %end
        # 错拿，随后所有响应整体错位。
        self._pending: "collections.deque[queue.Queue | None | object]" = collections.deque(
            [_STARTUP_WAITER]
        )
        # reader 已从 FIFO 取出、但守卫块尚未结束的当前请求。close/通道死亡时
        # 也必须唤醒它，不能只清理仍留在 _pending 里的排队请求。
        self._active_waiter: queue.Queue | None | object = None
        self._proc = subprocess.Popen(
            [*keepalive._BASE_ARGV, "-C", "attach", "-t", name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if not self._ready.wait(_CALL_TIMEOUT) or not self._startup_ok or self.dead:
            self.close()
            raise OSError(f"tmux 控制通道启动失败：{name}")
        self.pane_id = self._query_pane_id()  # %N，refresh -r 的寻址需要稳定 ID

    def _query_pane_id(self) -> str | None:
        known = _pane_ids.get(self.name)  # 创建时经 new-session -P 登记的最快
        if known:
            return known
        try:
            return subprocess.check_output(
                [*keepalive._BASE_ARGV, "display-message", "-p", "-t", self.name,
                 "#{pane_id}"],
                stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT,
            ).decode().strip() or None
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None

    def _read_loop(self) -> None:
        # 块解析进度只在 reader 线程内读写；pending/active waiter 与写线程、
        # close 共享，统一用 _lock 保护。
        in_block = False
        block_lines: list[str] = []
        block_guard: tuple[str, str] | None = None
        current_waiter: "queue.Queue | None | object" = None
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if in_block:
                    m = _GUARD_RE.match(line)
                    if (
                        m is not None
                        and m.group(1) in ("end", "error")
                        and (m.group(2), m.group(3)) == block_guard
                    ):
                        # 真正的块终止行——必须连时间戳+命令号一起匹配，不能只
                        # 看前缀：capture-pane 的响应块是命令的原始输出，不像
                        # %output 通知那样对内容做八进制转义，pane 里的真实文本
                        # （比如某个程序打印的一行 "%error: ..."）理论上可能撞上
                        # "%end"/"%error" 前缀，靠 (ts, num) 精确配对排除这种
                        # 误判——协议本身不转义命令响应，这是唯一可靠的判据。
                        ok = m.group(1) == "end"
                        if current_waiter is _STARTUP_WAITER:
                            self._startup_ok = ok
                            self._ready.set()
                        elif current_waiter is not None:
                            try:
                                current_waiter.put_nowait((ok, block_lines))
                            except queue.Full:
                                pass  # 调用方已超时放弃，静默丢弃迟到的响应
                        with self._lock:
                            if self._active_waiter is current_waiter:
                                self._active_waiter = None
                        in_block = False
                        block_lines = []
                        block_guard = None
                        current_waiter = None
                        continue
                    notification = self._handle_notification(line)
                    if notification == "exit":
                        break
                    if notification == "handled":
                        continue
                    block_lines.append(line)
                    continue
                m = _GUARD_RE.match(line)
                if m is not None and m.group(1) == "begin":
                    in_block = True
                    block_guard = (m.group(2), m.group(3))
                    block_lines = []
                    # 和写线程登记 waiter 使用同一把锁。写线程会先 append、再把
                    # 命令 flush 给 tmux，因此 reader 不可能看到响应却看不到登记。
                    with self._lock:
                        try:
                            current_waiter = self._pending.popleft()
                        except IndexError:
                            # 只可能是未知的服务端自发命令块；丢弃其正文但不让
                            # 后续业务 waiter 错位。
                            current_waiter = None
                        self._active_waiter = current_waiter
                    continue
                notification = self._handle_notification(line)
                if notification == "exit":
                    break
                if notification == "handled":
                    continue
                if line.startswith("%error"):
                    self.last_error = line
        except (OSError, ValueError):
            pass
        self._mark_dead()
        self._notify()  # 唤醒 capture 循环，让它感知通道死亡并回退轮询/判定会话死亡

    def _handle_notification(self, line: str) -> str | None:
        """处理可夹在命令响应块之间的控制模式异步通知。

        返回 ``"handled"`` 表示通知已消费，``"exit"`` 表示 reader 应退出，
        ``None`` 表示这是普通命令输出。通知不能混入 capture-pane 的正文。
        """
        parts = line.split(None, 1)
        kind = parts[0] if parts else ""
        if kind == "%exit":
            return "exit"
        if kind not in _ASYNC_NOTIFICATIONS:
            return None
        if kind in ("%output", "%extended-output"):
            self._notify()
        elif kind == "%pause":
            # 流控：服务端发现客户端来不及消费时暂停推送，立即经同一控制通道
            # 回 continue；其响应会按 FIFO 正常落到一个 fire-and-forget 占位。
            if len(parts) == 2:
                pane = parts[1].strip()
                self.command("refresh", "-A", f"{pane}:continue")
        return "handled"

    def _mark_dead(self) -> None:
        """标记通道死亡，并让全部同步请求立即失败返回。"""
        self.dead = True
        self._ready.set()
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            if self._active_waiter is not None:
                pending.append(self._active_waiter)
                self._active_waiter = None
        for waiter in pending:
            if waiter is not None and waiter is not _STARTUP_WAITER:
                try:
                    waiter.put_nowait((False, []))
                except queue.Full:
                    pass

    def _notify(self) -> None:
        if self.on_output is not None:
            try:
                self.on_output()
            except Exception:
                pass  # 回调（Event.set）不应失败；兜底保读线程不死

    def _send_command(self, cmd: str, waiter: "queue.Queue | None") -> bool:
        """写一条命令行并把 waiter 原子地登记进 FIFO 响应队列。

        waiter 为 None（command()/send() 的既有用法）表示调用方不关心响应，
        对应块到达时直接丢弃；写入与登记必须在同一把锁内完成，否则并发调用者
        之间可能出现「命令已经进了 tmux 的处理队列，但登记还没跟上」的窗口，
        使 _read_loop 按到达顺序 popleft 时配对到错误的调用方。
        """
        failed = False
        try:
            with self._lock:
                if self.dead or self._closed:
                    return False
                # 必须先登记再写入。reader 取 pending 也拿同一把锁；释放锁时命令
                # 已经 flush，因而不存在“响应先到、waiter 后登记”的窗口。
                self._pending.append(waiter)
                self._proc.stdin.write(cmd.encode("utf-8") + b"\n")
                self._proc.stdin.flush()
        except (OSError, ValueError):
            # 写失败时 reader 不可能为本命令收到响应；登记项仍在队尾，安全回滚。
            with self._lock:
                if self._pending and self._pending[-1] is waiter:
                    self._pending.pop()
            failed = True
        if failed:
            self._mark_dead()
            return False
        return True

    def send(self, cmd: str) -> bool:
        """写一条命令行；通道已死或写入失败返回 False（调用方回退 fork 路径）。"""
        return self._send_command(cmd, None)

    def command(self, *args: str) -> bool:
        return self.send(" ".join(_ctl_quote(a) for a in args))

    def request(self, *args: str, timeout: float = _CALL_TIMEOUT) -> list[str] | None:
        """同步发命令并等待其 %begin…%end/%error 响应块，返回块内文本行列表。

        用于 capture-pane/display-message 这类只读查询——通道存活时不必再为
        每次查询 fork 一个 tmux 客户端进程（原来的抓帧循环每帧都要付这个代价，
        真机实测是内嵌面板 CPU 占用的主要来源之一）。命令失败（%error）或
        通道死亡/超时都返回 None，调用方据此回退外部 fork 路径。
        """
        if self.dead:
            return None
        waiter: "queue.Queue[tuple[bool, list[str]]]" = queue.Queue(maxsize=1)
        cmd = " ".join(_ctl_quote(a) for a in args)
        if not self._send_command(cmd, waiter):
            return None
        try:
            ok, lines = waiter.get(timeout=timeout)
        except queue.Empty:
            # 控制响应超时后不能继续复用 FIFO：不知道这条响应是迟到还是已经
            # 丢失，继续发请求只会让后续响应整体错位、pending 无界增长。
            self.close()
            return None
        return lines if ok else None

    def close(self) -> None:
        """幂等关闭控制 client，唤醒请求方并完整回收子进程、管道和 reader。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._mark_dead()
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except OSError:
                pass
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass
            else:
                try:
                    self._proc.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if threading.current_thread() is not self._reader:
                self._reader.join(timeout=0.5)
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except OSError:
                pass


_channel: ControlChannel | None = None
_channel_lock = threading.Lock()


def open_channel(name: str, on_output=None) -> ControlChannel | None:
    """聚焦托管会话时打开控制通道；同时只维护一条，切换会话时自动关旧开新。

    同名复用时也要把 on_output 换成最新调用者传入的回调——host_session 会在
    真实命令跑起来前就为注入背景色调用一次（回调是 None），EmbedPane 稍后
    聚焦同一个会话时如果不更新回调，会一直沿用 None，导致「抓到新输出立即
    唤醒重绘」这个事件驱动机制失效、退化成慢速轮询。

    打开失败（tmux 缺失、会话刚退出）返回 None，全部发送路径自动回退外部 fork。
    """
    global _channel
    with _channel_lock:
        if _channel is not None and (_channel.dead or _channel.name != name):
            _channel.close()
            _channel = None
        if _channel is None:
            try:
                _channel = ControlChannel(name, on_output)
            except OSError:
                _channel = None
        elif on_output is not None:
            _channel.on_output = on_output
        return _channel


def close_channel() -> None:
    """关闭当前控制通道（分栏关闭、TUI 退出前必须调用，否则孤儿控制 client
    会一直挂在保活服务端上）。"""
    global _channel
    with _channel_lock:
        if _channel is not None:
            _channel.close()
            _channel = None


def _active_channel(name: str) -> ControlChannel | None:
    ch = _channel
    if ch is not None and not ch.dead and ch.name == name:
        return ch
    return None


def active_channel(name: str) -> ControlChannel | None:
    """_active_channel 的公开版，供 capture 循环判断事件驱动是否可用。"""
    return _active_channel(name)


def _modify(name: str, *args: str) -> None:
    """修改类 tmux 命令的统一入口：通道优先，通道死/缺时外部 fork 兜底。"""
    ch = _active_channel(name)
    if ch is not None and ch.command(*args):
        return
    try:
        subprocess.run(
            [*keepalive._BASE_ARGV, *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def sgr_mouse_sequence(button: int, x: int, y: int) -> str:
    """SGR 1006 鼠标事件序列（滚轮 64/65 只有 press 事件）；坐标为 1-based pane 内行列。"""
    return f"\x1b[<{button};{x};{y}M"


# ---- SGR 鼠标序列后台发送 ----
#
# 新版 Claude Code（v2.1.88 起默认全屏渲染并申请鼠标捕获）下，滚轮事件要转成
# SGR 序列直达内层程序。触控板惯性滚动一秒能产生上百个事件，而 send_literal
# 的 force_fork 路径每次约 10ms——在 UI 主线程同步发送会把整个界面堵死
# （2026-07-19 真机定位的滚动卡顿根因）。这里统一排队到专用后台线程发送：
# 主线程零 fork；积压超过上限时丢弃最旧事件——内层程序重绘不过来时，跳过
# 中间几步比停手后还在"追滚动"体验好，与 tmux 自身丢弃鼠标事件的策略一致。
# 发送仍走 force_fork 外部子进程（tmuxy 实测：转义密集的 SGR 内容走外部比
# 控制通道稳），只是不再阻塞调用方。

_WHEEL_SEND_INTERVAL = 0.02  # 发送限速（秒）：内层程序每个滚轮事件都要整屏重绘，更快没意义
_WHEEL_QUEUE_MAX = 12        # 单会话积压上限，超出丢最旧
_wheel_lock = threading.Lock()
_wheel_queues: dict[str, "collections.deque[str]"] = {}
_wheel_wake = threading.Event()
_wheel_thread: threading.Thread | None = None


def send_mouse_sequence(name: str, seq: str) -> None:
    """非阻塞发送 SGR 鼠标序列：排队到后台线程发送，UI 主线程可安全调用。"""
    global _wheel_thread
    with _wheel_lock:
        queue = _wheel_queues.setdefault(name, collections.deque())
        queue.append(seq)
        while len(queue) > _WHEEL_QUEUE_MAX:
            queue.popleft()
        if _wheel_thread is None or not _wheel_thread.is_alive():
            _wheel_thread = threading.Thread(
                target=_wheel_send_loop, daemon=True, name="embed-mouse-sender")
            _wheel_thread.start()
    _wheel_wake.set()


def _wheel_send_loop() -> None:
    while True:
        with _wheel_lock:
            name = next((n for n, q in _wheel_queues.items() if q), None)
            seq = _wheel_queues[name].popleft() if name is not None else None
            if name is not None and not _wheel_queues[name]:
                del _wheel_queues[name]
        if seq is None:
            # 无事时挂起等唤醒；5s 兜底心跳防止极端竞争下漏唤醒
            _wheel_wake.wait(5.0)
            _wheel_wake.clear()
            continue
        started = time.monotonic()
        send_literal(name, seq, force_fork=True)
        remaining = _WHEEL_SEND_INTERVAL - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


# ---- tmux 版本探测：硬性最低版本（pickup._require_tmux）+ 背景色注入软性检查 ----

# `new-session -e` 环境变量注入（PICKUP_RUNTIME/PICKUP_SESSION_ID 等托管元数据的
# 唯一注入点）与 pause-after 流控通知（%pause）都要求 tmux 3.2+（2021-04 发布）；
# 更旧的版本上 host_session()/wrap_plan() 会在创建会话时报一个笼统的
# EmbedError，看不出是版本问题。pickup._require_tmux() 用这个常量做硬性拦截。
MIN_TMUX_VERSION = (3, 2)


@functools.lru_cache(maxsize=1)
def _tmux_version() -> tuple[int, int] | None:
    """解析 tmux -V（"tmux 3.5a" / "tmux next-3.7"）为 (主, 次) 版本号。"""
    try:
        out = subprocess.check_output(["tmux", "-V"],
                                      stderr=subprocess.DEVNULL,
                                      timeout=_CALL_TIMEOUT).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def supports_theme_report() -> bool:
    """服务端是否支持 refresh-client -r（3.5a 引入）：决定背景色走注入还是文档兜底。"""
    version = _tmux_version()
    return version is not None and version >= (3, 5)


def report_theme(channel: ControlChannel, report: bytes) -> bool:
    """把外层终端的 OSC 10/11 应答原文注入 pane：此后 pane 内程序的背景色查询
    由 tmux 按真实终端值应答，agent 的深/浅主题自动检测恢复正常。

    只影响注入后发起查询的程序——已在运行的 agent 若启动时已完成检测，需重启
    进程或在其设置里手动固定主题。
    """
    if channel.pane_id is None:
        return False
    text = report.decode("ascii", errors="ignore")
    if not text:
        return False
    return channel.command("refresh", "-r", f"{channel.pane_id}:{text}")


# ---------------------------------------------------------------------------
# 按键翻译：Textual 按键事件的 key 名 → tmux send-keys 键名
# ---------------------------------------------------------------------------

_TEXTUAL_KEY_NAMES = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PPage",
    "pagedown": "NPage",
    "delete": "DC",
    "insert": "IC",
}
_TEXTUAL_KEY_NAMES.update({f"f{n}": f"F{n}" for n in range(1, 13)})


def translate_textual_key(key: str) -> tuple[str, str] | None:
    """把 Textual 按键事件的 key 名翻译成 ("keys", tmux 键名)；无法翻译返回 None。

    可打印字符经 Textual 的 event.character 直接走 send_literal，不经过这里；
    这里只处理控制键与特殊键名（Textual 的 key 是稳定字符串，如 "up"/"ctrl+c"，
    不再是 curses 那种平台相关的整数键码）。
    """
    if key in ("enter", "return"):
        return ("keys", "Enter")
    if key == "tab":
        return ("keys", "Tab")
    if key == "shift+tab":
        # tmux 没有 S-Tab：终端里 Shift+Tab 是 backtab，tmux 的具名键是 BTab
        # （tmux(1) 手册明确列出——大多数带 Shift 的键改用有专名的形式，不走
        # S- 前缀）。Claude Code 用这个键循环 plan/权限模式，是高频操作，漏掉
        # 会在内嵌面板里表现为「按了没反应」且没有任何提示（真实缺口，非假设）。
        return ("keys", "BTab")
    if key == "backspace":
        return ("keys", "BSpace")
    if key == "escape":
        return ("keys", "Escape")
    if key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
        return ("keys", f"C-{key[5]}")
    name = _TEXTUAL_KEY_NAMES.get(key)
    if name is not None:
        return ("keys", name)
    return None


# ---------------------------------------------------------------------------
# 画面解析：capture-pane -e 输出 → 定宽定高的字符单元格网格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    ch: str = " "
    # -1 = 终端默认色；0-255 = 256 色索引；tuple(r, g, b) = 真彩色直通（SGR
    # 38/48;2;r;g;b 原样保留，不再量化）——curses 时代的 `_rgb_to_256` 量化是
    # curses 颜色对（256 个上限）的限制，不是现在 Textual/Rich 渲染端的限制；
    # Rich 用 `Color.from_rgb` 原样表达任意 RGB，量化到这里只是无谓降质。
    fg: "int | tuple[int, int, int]" = -1
    bg: "int | tuple[int, int, int]" = -1
    bold: bool = False
    dim: bool = False
    underline: bool = False
    reverse: bool = False
    wide_cont: bool = False  # 宽字符（CJK/emoji）的第二个占位格，渲染时跳过


_BLANK = Cell()


def _char_width(ch: str) -> int:
    # 与 Rich/Textual 的渲染宽度表保持一致（`rich.cells.cell_len`）：自实现的
    # `unicodedata.east_asian_width` 在 emoji、组合字符、ambiguous-width 字符
    # 上会跟 Rich 的排版结果不一致（本项目同时用这两套计算，会导致内嵌画面
    # 里 CJK/emoji 对齐错位）。`cell_len("")` 已经是 0，不用特判。
    return _rich_cell_len(ch)


class _SgrState:
    """单行的 SGR 属性状态机：遇到字符就按当前属性落格。"""

    def __init__(self) -> None:
        # 与 Cell.fg/bg 同语义：-1 默认色 / 0-255 索引色 / (r, g, b) 真彩色
        self.fg: "int | tuple[int, int, int]" = -1
        self.bg: "int | tuple[int, int, int]" = -1
        self.bold = False
        self.dim = False
        self.underline = False
        self.reverse = False

    def apply(self, params: list[int]) -> None:
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.__init__()
            elif p == 1:
                self.bold = True
            elif p == 2:
                self.dim = True
            elif p == 4:
                self.underline = True
            elif p == 7:
                self.reverse = True
            elif p == 22:
                self.bold = self.dim = False
            elif p == 24:
                self.underline = False
            elif p == 27:
                self.reverse = False
            elif p == 39:
                self.fg = -1
            elif p == 49:
                self.bg = -1
            elif 30 <= p <= 37:
                self.fg = p - 30
            elif 40 <= p <= 47:
                self.bg = p - 40
            elif 90 <= p <= 97:
                self.fg = p - 90 + 8
            elif 100 <= p <= 107:
                self.bg = p - 100 + 8
            elif p in (38, 48):
                # 38/48 ; 5 ; n  或  38/48 ; 2 ; r ; g ; b
                if i + 2 < len(params) and params[i + 1] == 5:
                    value: "int | tuple[int, int, int]" = params[i + 2]
                    i += 2
                elif i + 4 < len(params) and params[i + 1] == 2:
                    value = (params[i + 2], params[i + 3], params[i + 4])
                    i += 4
                else:
                    i += 1  # 参数不全，跳过模式位继续
                    continue
                if p == 38:
                    self.fg = value
                else:
                    self.bg = value
            i += 1

    def cell(self, ch: str, wide_cont: bool = False) -> Cell:
        return Cell(ch, self.fg, self.bg, self.bold, self.dim,
                    self.underline, self.reverse, wide_cont)


def _parse_line(line: str, width: int) -> list[Cell]:
    row = [_BLANK] * width
    state = _SgrState()
    x = 0
    i = 0
    n = len(line)
    while i < n and x < width:
        ch = line[i]
        if ch == "\x1b":
            # capture-pane -e 只输出 SGR 序列；其他 CSI/ESC 序列一律跳过，防止把
            # 非属性序列的字母正文画进网格。
            if i + 1 < n and line[i + 1] == "[":
                j = i + 2
                while j < n and not ("@" <= line[j] <= "~"):
                    j += 1
                if j >= n:
                    break
                final = line[j]
                body = line[i + 2:j]
                if final == "m":
                    try:
                        params = [int(p) if p else 0 for p in body.split(";")] if body else [0]
                    except ValueError:
                        params = []
                    state.apply(params)
                i = j + 1
                continue
            # 非 CSI 的 ESC 序列（如字符集选择 ESC ( B）：按 ECMA-48 结构跳过——
            # ESC + 若干中间字节（0x20-0x2F）+ 一个最终字节（0x30-0x7E）。
            j = i + 1
            while j < n and " " <= line[j] <= "/":
                j += 1
            if j < n and "0" <= line[j] <= "~":
                j += 1
            i = j
            continue
        w = _char_width(ch)
        if w == 0:
            # 组合字符：附加到前一个实格，行首则丢弃
            if x > 0 and not row[x - 1].wide_cont:
                prev = row[x - 1]
                row[x - 1] = Cell(prev.ch + ch, prev.fg, prev.bg, prev.bold,
                                  prev.dim, prev.underline, prev.reverse)
            i += 1
            continue
        row[x] = state.cell(ch)
        if w == 2:
            if x + 1 >= width:
                row[x] = _BLANK  # 宽字符被右边界切断：整格留空，避免半个字
                x += 1
            else:
                row[x + 1] = state.cell(" ", wide_cont=True)
                x += 2
        else:
            x += 1
        i += 1
    return row


def parse_screen(text: str, width: int, height: int) -> list[list[Cell]]:
    """把 capture-pane -e 的输出解析成 height×width 的单元格网格；行数不足补空行。"""
    lines = text.split("\n")
    grid = [_parse_line(line, width) for line in lines[:height]]
    while len(grid) < height:
        grid.append([_BLANK] * width)
    return grid


# ---------------------------------------------------------------------------
# 画面单元格 → Rich Style：渲染框架中立，供任意能画 Rich Text 的界面复用
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=4096)
def cell_style(cell: Cell) -> Style:
    """把一个单元格的颜色/属性组合编译成 Rich Style；结果按组合缓存（Cell 是
    frozen dataclass，可哈希），避免每帧对同一批组合重复构造 Style 对象。

    fg/bg 两种形态分别处理：0-255 的索引色直接 `Color.from_ansi`，(r, g, b)
    真彩色用 `Color.from_rgb` 原样传递——curses 时代必须量化到 256 色是因为
    颜色对上限是 256，这个限制在 Textual/Rich 渲染端不存在了，量化到这里只是
    无谓降质（托管 agent 的渐变/主题色会全被打到近似色上）。
    """
    if isinstance(cell.fg, tuple):
        fg_color = Color.from_rgb(*cell.fg)
    elif cell.fg >= 0:
        fg_color = Color.from_ansi(cell.fg)
    else:
        fg_color = None
    if isinstance(cell.bg, tuple):
        bg_color = Color.from_rgb(*cell.bg)
    elif cell.bg >= 0:
        bg_color = Color.from_ansi(cell.bg)
    else:
        bg_color = None
    return Style(
        color=fg_color,
        bgcolor=bg_color,
        bold=cell.bold or None,
        dim=cell.dim or None,
        underline=cell.underline or None,
        reverse=cell.reverse or None,
    )


def row_text_and_spans(row: list[Cell]) -> tuple[str, list[tuple[int, int, Style]]]:
    """单行 Cell 编译为 (纯文本, 样式段列表)——样式段是 (起始字符下标, 结束字符
    下标, Style)，下标按 `chars`（跳过 wide_cont 占位格后的纯文本）计数，不是
    终端列位置；相邻同样式单元格合并成一段。

    这是 `grid_to_text`（整屏一次性构造，供旧的整体 Rich Text 渲染路径）和
    `ui/embed_pane.py` 的 Line API 按行局部重绘（`render_line`，每行按需构造
    Textual `Strip`）共用的核心合并逻辑，抽成单行函数避免两处各自实现、
    渐渐演化出细微差异。保持在 `embed.py`（不 import textual）维持模块与 UI
    框架无关这条既有边界——`Strip`/`Segment` 这类 Textual 专属类型的构造留在
    `ui/embed_pane.py`。
    """
    chars: list[str] = []
    spans: list[tuple[int, int, Style]] = []
    x = 0
    span_start = 0
    span_style: Style | None = None
    for cell in row:
        if cell.wide_cont:
            continue
        chars.append(cell.ch)
        style = cell_style(cell)
        if style != span_style:
            if span_style is not None and x > span_start:
                spans.append((span_start, x, span_style))
            span_start = x
            span_style = style
        # span 下标供 Rich Text / Python 字符串切片使用，必须按 Python 字符索引
        # 累加，不能按终端 cell 数累加。组合字符会和基础字符合并在同一个 Cell.ch
        # 中（如 "e\u0301" 长度为 2）；仍然只加 1 会让后续文本被切掉。宽字符的
        # continuation cell 已在上面跳过，主 cell.ch 长度仍为 1，不受影响。
        x += len(cell.ch)
    if span_style is not None and x > span_start:
        spans.append((span_start, x, span_style))
    return "".join(chars), spans


def grid_to_text(grid: list[list[Cell]]) -> list[tuple[str, list[tuple[int, int, Style]]]]:
    """把单元格网格编译成每行 (纯文本, 样式段列表) ——样式段是 (起始列, 结束列, Style)，
    相邻同样式单元格合并成一段。调用方（如 Textual 的 EmbedPane）据此构造
    `rich.text.Text` 并按段调用 `stylize`，避免逐格构造 Style/Span 的开销。
    """
    return [row_text_and_spans(row) for row in grid]
