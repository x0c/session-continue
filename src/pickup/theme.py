"""外层终端主题探测与运行时标签配色。"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import time
import tty

# 侧边栏 / 详情里 runtime 名配色：按 id 高区分度着色（不必严格品牌色）。
RUNTIME_LABEL_STYLES = {
    "claude": "#D97757",
    "codex": "#60A5FA",
    "cursor": "#A78BFA",
    "kimi": "#F472B6",
    "opencode": "#34D399",
}


def runtime_label_style(runtime_id: str) -> str:
    """Rich/Textual 样式串：已知 runtime 用粗体品牌区分色，未知回退 dim。"""
    color = RUNTIME_LABEL_STYLES.get(str(runtime_id or ""))
    return f"bold {color}" if color else "dim"


# 外层终端 OSC 10/11 应答原文（main() 启动时探测），供内嵌面板聚焦时经
# refresh-client -r 注入托管 pane——pane 内 agent 的深/浅主题自动检测因此拿到真实终端背景。
_OSC_REPORT: bytes | None = None


def _probe_osc_colours(timeout: float = 1.2) -> bytes | None:
    """启动时向外层终端查询前景/背景色（OSC 10/11），返回应答原文字节串。

    tmux 默认不应答 pane 内的 OSC 11 查询（实测：agent 在 pane 里查询石沉大海，
    深/浅主题检测只能瞎猜）；tmux 3.5a+ 的 refresh-client -r 允许把真实终端的
    应答转注入 pane，这里先趁 Textual 接管终端前向用户终端要到应答原文。
    pickup 自己跑在 tmux 里时，学 Claude Code 的做法同时发 DCS passthrough 包装
    的查询——外层 tmux 开 allow-passthrough 时可穿透直达真实终端；裸查询部分由
    外层 tmux 用其 client 缓存值应答（3.4+）。非 TTY、终端不应答（超时）时返回
    None。测试钩子：PICKUP_OSC_REPORT（hex 编码）。
    """
    hook = os.environ.get("PICKUP_OSC_REPORT", "")
    if hook:
        try:
            return bytes.fromhex(hook)
        except ValueError:
            pass  # 钩子内容非法时按未设置处理，继续真实探测
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return None
    buf = bytearray()
    try:
        tty.setraw(fd)
        os.write(sys.stdout.fileno(), b"\x1b]10;?\a\x1b]11;?\a")
        if os.environ.get("TMUX"):
            # 内层 ESC 双写是 tmux DCS passthrough 的转义规则（Claude Code 同款）
            os.write(sys.stdout.fileno(),
                     b"\x1bPtmux;\x1b\x1b]10;?\x07\x1b\\"
                     b"\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and buf.count(b"rgb:") < 2:
            r, _, _ = select.select([fd], [], [], max(0.05, deadline - time.monotonic()))
            if not r:
                break
            buf += os.read(fd, 256)
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # 清空可能残留在输入队列里的 OSC 应答尾巴：读循环可能在应答完整到达前
        # 就因计数够/超时而退出（tmux 多段应答、SSH 往返晚于超时时尤甚），剩下的
        # 半截字节若留到 Textual 接管后会被当成键盘输入注入搜索框——表现为屏幕先
        # 闪过一行 `...rgb:xxxx/...`、搜索框乱码、且乱字符实时筛选把会话列表整个
        # 过滤空。恢复 termios 后无条件丢弃输入队列，杜绝这条泄漏路径。
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
    # 只保留 OSC 10/11 应答段，混入的用户按键等杂字节一律丢弃；passthrough 应答
    # 绕行真实终端通常晚于外层 tmux 的缓存应答，拼接在后，tmux 解析时后者生效
    parts = re.findall(rb"\x1b\](?:10|11);[^\x07\x1b]+(?:\x07|\x1b\\)", bytes(buf))
    return b"".join(parts) or None


def _background_channels(osc_report: bytes | None) -> tuple[float, float, float] | None:
    """从 OSC 11（背景色）应答解析出终端真实背景色的 (r, g, b) 三通道（各 0~1）；解析不出返回 None。

    应答形如 `\\x1b]11;rgb:1e1e/1e1e/2e2e\\x07`（每个通道 2 或 4 位十六进制）。
    取应答里最后一段 11; 匹配——同一探测里可能混入 tmux passthrough 的重复应答，
    最后一段通常是真实终端而非 tmux 缓存值。
    """
    if not osc_report:
        return None
    matches = re.findall(rb"\x1b\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", osc_report)
    if not matches:
        return None
    r_hex, g_hex, b_hex = matches[-1]
    try:
        channels = []
        for hex_part in (r_hex, g_hex, b_hex):
            value = int(hex_part, 16)
            max_value = (16 ** len(hex_part)) - 1
            channels.append(value / max_value)
    except (ValueError, ZeroDivisionError):
        return None
    return channels[0], channels[1], channels[2]


def _background_is_light(osc_report: bytes | None) -> bool | None:
    """从 OSC 11 应答判断终端是浅色还是深色背景；解析不出返回 None。

    亮度用 ITU-R BT.709 相对亮度公式（Claude Code 等同类工具同款算法），
    阈值 0.5：高于视为浅色背景。
    """
    channels = _background_channels(osc_report)
    if channels is None:
        return None
    r, g, b = channels
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return luminance > 0.5


def _background_rgb(osc_report: bytes | None) -> str | None:
    """从 OSC 11 应答解析出终端真实背景色的 `#rrggbb` 十六进制串；解析不出返回 None。

    内嵌面板用它把"默认背景"单元格（tmux 报 -1 的格子）渲染在外层终端真实底色上，
    而不是透出 Textual 主题的中性灰底——那样会让整个托管 Agent 画面看着变灰。
    """
    channels = _background_channels(osc_report)
    if channels is None:
        return None
    r, g, b = (max(0, min(255, round(c * 255))) for c in channels)
    return f"#{r:02x}{g:02x}{b:02x}"
