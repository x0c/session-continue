"""内嵌宿主：把托管在 tmux 里的会话画面搬进 pickup 自己的 curses 界面。

与 keepalive.py 平级的运行时无关层：keepalive 管「把启动计划包进 tmux 以便
SSH 断线保活」，本模块管「不 attach——用 capture-pane 拿画面、send-keys 送按键」，
让 pickup.py 的 TUI 回车后退化为左侧会话列表 + 右侧会话现场，会话在后台 tmux 里
持续运行，随时经列表切换。与保活共用 `tmux -L pickup-keepalive` socket 和
pickup-* 命名空间：e 键全屏接管、keepalive.annotate() 状态标注、reap_idle() 空闲
回收对内嵌会话全部照旧生效。适配器不感知本模块，pickup.py 主循环是唯一直接调用方。

渲染保真度由 tmux 自己保证（它就是终端模拟器）：本模块只解析 capture-pane -e
输出的 SGR 颜色序列，不做完整 VT100 模拟。
"""

from __future__ import annotations

import curses
import functools
import re
import shutil
import subprocess
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

import keepalive
from models import LaunchPlan

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


def host_session(plan: LaunchPlan, runtime_id: str, ident: str, width: int, height: int) -> str:
    """把启动计划以 detached 方式托管进保活 socket，返回 tmux 会话名。

    与 keepalive.wrap_plan 同命名空间、同环境变量注入；区别在不 attach（-d），
    并按面板实际尺寸创建（-x/-y），避免先 80x24 再 resize 的重排闪烁。
    会话名已存在（极端情况下同名残留）时视为复用，直接返回名字。
    创建时经 -P 顺带取回 pane_id 记入 _pane_ids——refresh -r 背景色注入需要
    pane_id 寻址，创建时就拿可以省掉一次 display-message 往返，让注入抢在
    agent 启动主题检测之前完成。
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
    """
    argv = [*keepalive._BASE_ARGV, "capture-pane", "-p", "-e", "-t", name]
    if scroll_offset > 0 and pane_height > 0:
        # 实测钉死的窗口公式：-S -offset -E (h-1-offset) = live 窗口精确上移 offset 行
        # （seq 1 100 会话里 -S -6 -E 13 得 76..95，相对 live 82..101 正好上移 6）
        argv[4:4] = ["-S", f"-{scroll_offset}", "-E", str(pane_height - 1 - scroll_offset)]
    try:
        out = subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.decode("utf-8", errors="replace")


def pane_state(name: str) -> tuple[int, int, bool, bool, bool, int] | None:
    """一次查询拿全 pane 交互状态：(光标 x, 光标 y, 光标可见, 程序申请了鼠标,
    SGR 鼠标模式, 回滚行数 history_size)。

    合并进单个 display-message 调用——capture 循环每轮都要光标位置，滚轮转发要鼠标
    模式，应用层滚动的上限判定要回滚量，分开查每轮就是三次 fork。查询失败返回 None。
    """
    try:
        out = subprocess.check_output(
            [*keepalive._BASE_ARGV, "display-message", "-p", "-t", name,
             "#{cursor_x}|#{cursor_y}|#{cursor_flag}|#{mouse_any_flag}|#{mouse_sgr_flag}"
             "|#{history_size}"],
            stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        xs, ys, fs, ma, ms, hs = out.split("|")
        return int(xs), int(ys), fs == "1", ma == "1", ms == "1", int(hs)
    except ValueError:
        return None


def resize(name: str, width: int, height: int) -> None:
    """把托管会话的窗口调整为面板尺寸；失败静默（会话可能刚好退出）。"""
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


class ControlChannel:
    """到某个托管会话的控制模式通道。

    stdin 写命令（tmux 命令行语法，参数经 _ctl_quote）；stdout 由读线程按行协议
    消费：%output → on_output 回调（capture 循环的抓帧信号）、%pause → 自动回
    refresh -A 恢复（tmux 3.2+ pause-after 流控）、%exit/EOF → 标记死亡。
    """

    def __init__(self, name: str, on_output=None) -> None:
        self.name = name
        self.on_output = on_output
        self.dead = False
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [*keepalive._BASE_ARGV, "-C", "attach", "-t", name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.pane_id = self._query_pane_id()  # %N，refresh -r 的寻址需要稳定 ID
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

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
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if line.startswith("%output "):
                    self._notify()
                elif line.startswith("%pause "):
                    # 流控：服务端发现我们落后 pause-after 秒未消费时暂停推送，
                    # 回 continue 恢复（输出内容本身不解析，不存在真正的积压）
                    pane = line.split(None, 1)[1].strip()
                    self.command("refresh", "-A", f"{pane}:continue")
                elif line.startswith("%error"):
                    self.last_error = line
                elif line.startswith("%exit"):
                    break
        except (OSError, ValueError):
            pass
        self.dead = True
        self._notify()  # 唤醒 capture 循环，让它感知通道死亡并回退轮询/判定会话死亡

    def _notify(self) -> None:
        if self.on_output is not None:
            try:
                self.on_output()
            except Exception:
                pass  # 回调（Event.set）不应失败；兜底保读线程不死

    def send(self, cmd: str) -> bool:
        """写一条命令行；通道已死或写入失败返回 False（调用方回退 fork 路径）。"""
        if self.dead:
            return False
        try:
            with self._lock:
                self._proc.stdin.write(cmd.encode("utf-8") + b"\n")
                self._proc.stdin.flush()
        except (OSError, ValueError):
            self.dead = True
            return False
        return True

    def command(self, *args: str) -> bool:
        return self.send(" ".join(_ctl_quote(a) for a in args))

    def close(self) -> None:
        self.dead = True
        try:
            self._proc.kill()
        except OSError:
            pass


_channel: ControlChannel | None = None
_channel_lock = threading.Lock()


def open_channel(name: str, on_output=None) -> ControlChannel | None:
    """聚焦托管会话时打开控制通道；同时只维护一条，切换会话时自动关旧开新。

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


# ---- 终端背景色注入：refresh-client -r（tmux 3.5a+）----

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
# 按键翻译：curses getch 返回的键码 → tmux send-keys 键名 / 字面文本
# ---------------------------------------------------------------------------

_KEY_NAMES = {
    curses.KEY_UP: "Up",
    curses.KEY_DOWN: "Down",
    curses.KEY_LEFT: "Left",
    curses.KEY_RIGHT: "Right",
    curses.KEY_HOME: "Home",
    curses.KEY_END: "End",
    curses.KEY_PPAGE: "PPage",
    curses.KEY_NPAGE: "NPage",
    curses.KEY_DC: "DC",
    curses.KEY_IC: "IC",
    curses.KEY_BACKSPACE: "BSpace",
}
_KEY_NAMES.update({getattr(curses, f"KEY_F{n}"): f"F{n}" for n in range(1, 13)})


def translate_key(ch: int) -> tuple[str, str] | None:
    """把 curses 键码翻译成 ("literal", 文本) 或 ("keys", tmux 键名)；无法翻译返回 None。

    可打印 ASCII 与高位字节（UTF-8 片段，由调用方先按字节攒批、解码后再调
    send_literal）不经过这里；这里只处理控制键与特殊键。
    """
    if ch in (10, 13, curses.KEY_ENTER):
        return ("keys", "Enter")
    if ch == 9:
        return ("keys", "Tab")
    if ch in (8, 127):
        return ("keys", "BSpace")
    if ch == 27:
        return ("keys", "Escape")
    if 1 <= ch <= 26:
        return ("keys", f"C-{chr(ord('a') + ch - 1)}")
    name = _KEY_NAMES.get(ch)
    if name is not None:
        return ("keys", name)
    return None


# ---------------------------------------------------------------------------
# 画面解析：capture-pane -e 输出 → 定宽定高的字符单元格网格
# ---------------------------------------------------------------------------

def _rgb_to_256(r: int, g: int, b: int) -> int:
    """真彩色量化到 xterm 256 色；curses 端无法表达任意 RGB，近似即可。"""
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    cube = lambda v: round(v / 255 * 5)  # noqa: E731
    return 16 + 36 * cube(r) + 6 * cube(g) + cube(b)


@dataclass(frozen=True)
class Cell:
    ch: str = " "
    fg: int = -1   # 0-255；-1 = 终端默认前景
    bg: int = -1   # 0-255；-1 = 终端默认背景
    bold: bool = False
    dim: bool = False
    underline: bool = False
    reverse: bool = False
    wide_cont: bool = False  # 宽字符（CJK/emoji）的第二个占位格，渲染时跳过


_BLANK = Cell()


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    return 1


class _SgrState:
    """单行的 SGR 属性状态机：遇到字符就按当前属性落格。"""

    def __init__(self) -> None:
        self.fg = -1
        self.bg = -1
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
                    value = params[i + 2]
                    i += 2
                elif i + 4 < len(params) and params[i + 1] == 2:
                    value = _rgb_to_256(params[i + 2], params[i + 3], params[i + 4])
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
# curses 颜色对池：内嵌画面的 fg/bg 组合不可预知，按需 init_pair + LRU 复用
# ---------------------------------------------------------------------------

class PairPool:
    """动态颜色对分配器。

    curses 的 COLOR_PAIRS 有限（常见 256 或 32767），pickup.py 的静态颜色对占用前
    15 个，池子从 first 开始分配。组合数超出容量时按 LRU 回收颜色对编号重新
    init_pair；连默认背景组合都放不下时退化为无颜色，保证不崩。
    """

    def __init__(self, first: int = 16, use_default: bool = True):
        self.first = first
        self.use_default = use_default
        self.capacity = max(0, min(curses.COLOR_PAIRS, 512) - first)
        self._pairs: OrderedDict[tuple[int, int], int] = OrderedDict()

    def _color(self, value: int, fallback: int) -> int:
        if value == -1 and not self.use_default:
            return fallback
        return value

    def _pair_number(self, fg: int, bg: int) -> int | None:
        key = (fg, bg)
        existing = self._pairs.get(key)
        if existing is not None:
            self._pairs.move_to_end(key)
            return existing
        if not self.capacity:
            return None
        if len(self._pairs) >= self.capacity:
            _, number = self._pairs.popitem(last=False)  # 回收最久未用的编号
        else:
            number = self.first + len(self._pairs)
        try:
            curses.init_pair(number, fg, bg)
        except curses.error:
            return None
        self._pairs[key] = number
        return number

    def attr(self, cell: Cell) -> int:
        attr = 0
        if cell.bold:
            attr |= curses.A_BOLD
        if cell.dim:
            attr |= curses.A_DIM
        if cell.underline:
            attr |= curses.A_UNDERLINE
        if cell.reverse:
            attr |= curses.A_REVERSE
        fg = self._color(cell.fg, curses.COLOR_WHITE)
        bg = self._color(cell.bg, curses.COLOR_BLACK)
        if fg == -1 and bg == -1:
            return attr
        number = self._pair_number(fg, bg)
        if number is None:
            # 池满：先丢背景色再试一次，仍失败就放弃颜色只留属性
            if bg != -1:
                number = self._pair_number(fg, -1 if self.use_default else curses.COLOR_BLACK)
            if number is None:
                return attr
        return attr | curses.color_pair(number)
