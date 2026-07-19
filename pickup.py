#!/usr/bin/env python3
"""pickup：终端会话接力工具。

单列表格列出已注册运行时（Claude Code / Codex / OpenCode / Kimi Code）的最近会话，左右切换来源、
上下选行。回车把会话内嵌到右半屏（托管在后台 tmux，左侧列表退化为窄栏，可多会话并行切换）；
按 e 则走经典全屏接管。跨运行时可通过高级操作交给其他运行时接力。tmux 为硬依赖。

注意：默认启动交互式终端 TUI（Textual），需要真实终端，不能被自动化脚本或
大模型直接调用。非真实终端环境（管道、脚本、Agent 调用）会自动退化为 JSON
会话列表。大模型 Agent 需要结构化查询（列表/搜索/详情/接续上下文/续接计划）时，使用
`pickup list` / `pickup search` / `pickup show` / `pickup context` / `pickup plan continue` / `pickup describe` 子命令，
详见 agent_api.py 和 docs/SKILL.md。

用法：
    pickup                  # 启动 TUI（交互式，需要真实终端）
    pickup --limit 30        # 每个来源最多列出 30 条
    pickup --json            # 输出 JSON 会话列表后退出，不启动 TUI（旧格式，仍保留）
    pickup --json --limit 5  # JSON 模式，每个来源最多 5 条
    pickup list              # 结构化会话列表（推荐给 Agent 使用，字段更完整）
    pickup search 天气 app    # 按关键词搜会话
    pickup show <会话ID前缀>  # 查看会话详情和对话内容
    pickup context <会话ID前缀>  # 生成接续该会话所需的上下文数据包
    pickup plan continue <会话ID前缀> --instruction "继续完成剩余工作"  # 只生成后台续接计划
    pickup describe          # 查看全部子命令的参数与输出字段说明
    pickup claude [参数…]     # 直启：新建 Claude 会话，参数原样透传，默认全自动放行+后台保活
    pickup codex [参数…]      # 直启：新建 Codex 会话，同上
    pickup kimi [参数…]       # 直启：新建 Kimi 会话，同上
    pickup --no-keepalive claude [参数…]  # 直启但不包后台保活
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import traceback
import tty
import unicodedata
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 关掉 Textual 默认开启的 Kitty 键盘协议（DISAMBIGUATE | REPORT_ALL_KEYS |
# REPORT_ASSOCIATED_TEXT，见 textual 的 linux_driver）。这个协议会让支持它的终端
# （iTerm2 / Ghostty / kitty 等）把按键当转义码「原样上报」，从而绕过操作系统的
# 输入法（IME）——用户在内嵌 Agent 里打 `nihao` 时，终端把 n/i/h/a/o 直接作为
# CSI-u 事件发给应用，IME 根本没机会介入弹候选词，导致中文压根打不出来（真机
# 反馈：iTerm2 + SSH 下内嵌 Agent 无法输入中文，同一 SSH 里的 nano 却正常——
# 唯一差别就是 nano 没开这个协议）。pickup 本质是个把外层终端输入转发给托管
# tmux 会话的终端复用器（类似 tmux/screen，它们默认也不对外层开 Kitty 协议），
# 普通字节 + 标准转义序列已经够用，Kitty 协议带来的按键消歧义好处对 pickup 边际
# 很小，却实打实破坏 IME，因此默认关闭。必须在任何 `import textual` 之前设置
# （textual.constants 在导入时就把这个开关读成 Final 常量）；ui.app 是延迟导入，
# 这里在模块顶层设好即可。用 setdefault：想恢复协议的用户可显式设成非 "1" 值。
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

import agent_api
import embed
import keepalive
import titles
from models import (
    ConversationMessage,
    LaunchPlan,
    LaunchRequest,
    NewSessionRequest,
    format_message_time,
    session_key,
)
from runtime import LaunchError, RuntimeRegistry, default_registry, execute_launch, usable_cwd


def _format_relative_time(mtime: float, now: float | None = None) -> str:
    """把时间戳渲染成人性化相对时间；超过一天退回绝对日期时间。

    展示层专用，只在 TUI 渲染时现算，不写回 display_time（后者保持绝对格式，
    供 --json 与单测稳定消费）。
    """
    if now is None:
        now = datetime.now().timestamp()
    delta = now - mtime
    if delta < 60:  # 含未来时间 / 时钟漂移导致的负值
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    return 1


def _text_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _fit_cell(text: object, width: int) -> str:
    """按终端显示宽度截断并补齐，避免中文和图标把表格列挤歪。"""
    if width <= 0:
        return ""
    out = []
    used = 0
    for ch in str(text):
        ch_width = _char_width(ch)
        if used + ch_width > width:
            break
        out.append(ch)
        used += ch_width
    return "".join(out) + " " * (width - used)


def _fit_cell_right(text: object, width: int) -> str:
    """按终端显示宽度截断并右对齐补齐（数值列用）。"""
    if width <= 0:
        return ""
    out = []
    used = 0
    for ch in str(text):
        ch_width = _char_width(ch)
        if used + ch_width > width:
            break
        out.append(ch)
        used += ch_width
    return " " * (width - used) + "".join(out)


def _wrap_preview_text(text: str, width: int) -> list[str]:
    """按终端显示宽度折行，并移除会破坏 TUI 的控制字符。"""
    if width <= 0:
        return []

    cleaned = "".join(
        ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else " "
        for ch in text
    ).replace("\t", "    ")
    lines: list[str] = []
    for paragraph in cleaned.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current: list[str] = []
        used = 0
        for ch in paragraph:
            ch_width = _char_width(ch)
            if current and used + ch_width > width:
                lines.append("".join(current))
                current = []
                used = 0
            current.append(ch)
            used += ch_width
        lines.append("".join(current))
    return lines


def _preview_lines(
    messages: list[ConversationMessage], runtime_name: str, width: int,
) -> list[tuple[str, str, str]]:
    """把真实会话消息整理为带角色样式的聊天记录行。

    每行是 (kind, text, dim_suffix) 三元组：角色行的 dim_suffix 携带发送时间（用淡色
    单独叠绘，不和角色名共用同一个高亮色），消息缺时间戳（老格式历史）或其余行留空。
    """
    content_width = max(1, width - 2)
    if not messages:
        return [("dim", "没有可预览的用户消息或最终答复", "")]

    lines: list[tuple[str, str, str]] = []
    for message in messages:
        if lines:
            lines.append(("blank", "", ""))
        time_suffix = f"  · {format_message_time(message.timestamp)}" if message.timestamp else ""
        if message.role == "user":
            lines.append(("user", "● 你", time_suffix))
        else:
            lines.append(("assistant", f"◆ {runtime_name}", time_suffix))
        lines.extend(
            ("body", f"  {line}", "")
            for line in _wrap_preview_text(message.text.strip(), content_width)
        )
    return lines


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille 转圈圈，每帧占 1 列宽

UNKNOWN_PROJECT_LABEL = "(未知目录)"


def _normalize_cwd(cwd: object) -> str:
    """把工作目录归一化为分组/过滤用的唯一键；空值或根目录归一为空字符串。"""
    text = str(cwd or "").strip()
    if not text:
        return ""
    normalized = os.path.normpath(text)
    if normalized in (".", "/"):
        return ""
    return normalized


def _disambiguate_labels(cwd_keys: list[str]) -> dict[str, str]:
    """同名末级目录逐级向上补父级路径，直到唯一（VS Code 标签页风格）。"""
    parts = {key: [p for p in key.split("/") if p] for key in cwd_keys}
    depth = {key: 1 for key in cwd_keys}
    labels: dict[str, str] = {}

    while True:
        labels = {}
        for key in cwd_keys:
            segments = parts[key]
            d = min(depth[key], len(segments)) if segments else 0
            labels[key] = "/".join(segments[-d:]) if d else key

        groups: dict[str, list[str]] = {}
        for key, label in labels.items():
            groups.setdefault(label, []).append(key)

        changed = False
        for members in groups.values():
            if len(members) <= 1:
                continue
            for key in members:
                if depth[key] < len(parts[key]):
                    depth[key] += 1
                    changed = True
        if not changed:
            return labels


def _project_groups(sessions_by_source: dict[str, list[dict]]) -> list[dict]:
    """合并所有来源的会话，按工作目录分组统计，用于侧边栏展示。

    每项：{"cwd_key": 完整归一化路径（过滤用，"" 表示未知目录）,
           "label": 去歧义后的显示名, "count": 会话数, "latest_mtime": 最近会话时间}。
    排序：会话数倒序 → 最近会话时间倒序 → 显示名字典序（稳定兜底）。
    """
    groups: dict[str, dict] = {}
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = _normalize_cwd(session.get("cwd"))
            entry = groups.setdefault(key, {"cwd_key": key, "count": 0, "latest_mtime": 0.0})
            entry["count"] += 1
            mtime = session.get("mtime") or 0
            if mtime > entry["latest_mtime"]:
                entry["latest_mtime"] = mtime

    named_keys = [key for key in groups if key]
    labels = _disambiguate_labels(named_keys)
    for key in named_keys:
        groups[key]["label"] = labels[key]
    if "" in groups:
        groups[""]["label"] = UNKNOWN_PROJECT_LABEL

    return sorted(groups.values(), key=lambda p: (-p["count"], -p["latest_mtime"], p["label"]))


def _filter_sessions(sessions: list[dict], cwd_key: str | None) -> list[dict]:
    """按归一化工作目录精确匹配过滤；cwd_key 为 None 时原样返回（不过滤）。"""
    if cwd_key is None:
        return sessions
    return [s for s in sessions if _normalize_cwd(s.get("cwd")) == cwd_key]


class SessionStore:
    """持有所有已注册运行时的会话列表与标题缓存。

    标题生成已移交独立后台进程（pickup --generate-titles），本类只负责读取缓存，
    并通过轮询缓存文件把后台进程逐批写入的新标题反映到界面，自身不写缓存、
    不调用 claude，避免与后台进程重复花额度或竞争缓存文件。
    """

    def __init__(self, limit: int, registry: RuntimeRegistry | None = None):
        self.limit = limit
        self.registry = registry or default_registry()
        self.lock = threading.Lock()
        self.sessions: dict[str, list[dict]] = {runtime_id: [] for runtime_id in self.registry.ids}
        self.display_titles: dict[str, str] = {}  # 跨运行时会话键 -> 当前展示标题
        self.dirty = threading.Event()
        self.cache = titles.load_cache()
        self.generating: set[str] = set()  # 仍是临时兜底、等待后台进程产出的会话键（转圈圈）
        # 本进程内嵌托管的 会话键 -> tmux 会话名。_embed_open 在启动成功的瞬间就写入，
        # 比 annotate() 的 pid 祖先链匹配更快、更确定：运行时还没来得及注册 pid 文件
        # （或像某些 fake CLI 一样根本不注册）时，后台重扫替换会话字典后仍能立刻恢复
        # keepalive_name，避免 x 拒绝关闭、回车误开竞争进程。
        self.hosted: dict[str, str] = {}
        # 值是 (读取时的历史文件 mtime, 消息列表)；文件 mtime 变化就重读，
        # 修掉"同一次 pickup 内 / 关闭预览重开还是旧内容"的问题。
        self.conversations: dict[str, tuple[float | None, list[ConversationMessage]]] = {}
        self._cache_mtime: float = self._cache_file_mtime()
        self._projects: list[dict] | None = None  # 项目聚合缓存，仅在 load() 时失效
        # 稳定的展示顺序（跨运行时会话键）：列表展示出来后已有会话位置固定，
        # 后台重扫只把「新出现」的会话插到最前，不再按 mtime 整体重排——
        # 否则运行中的会话一有消息更新就跳到列表顶上，用户刚要看的位置全乱（用户实报）。
        self._order: list[str] = []

    @staticmethod
    def _cache_file_mtime() -> float:
        try:
            return os.path.getmtime(titles.CACHE_FILE)
        except OSError:
            return 0.0

    def load(self) -> None:
        scanned = self.registry.scan_all(self.limit)
        self._merge_scanned(scanned)

    def refresh(self) -> bool:
        """后台周期性重扫磁盘，把新增/结束的会话并入当前列表。

        与 load() 共用合并逻辑，唯一区别是返回「会话集合是否真的变了」，
        供调用方只在有变化时才 dirty.set()，避免主循环无谓重定位光标。
        """
        scanned = self.registry.scan_all(self.limit)
        before = self._sessions_signature()
        self._merge_scanned(scanned)
        return self._sessions_signature() != before

    def _sessions_signature(self) -> tuple:
        with self.lock:
            return tuple(
                (runtime_id, tuple(session_key(session) for session in bucket))
                for runtime_id, bucket in sorted(self.sessions.items())
            )

    def _merge_scanned(self, scanned: dict[str, list[dict]]) -> None:
        # 每个适配器负责按时间倒序返回，无需在界面层二次排序
        keepalive.annotate([session for bucket in scanned.values() for session in bucket])

        with self.lock:
            self.sessions.update(scanned)
            by_key: dict[str, dict] = {}
            for bucket in self.sessions.values():
                for session in bucket:
                    by_key[session_key(session)] = session
            # 稳定顺序：已展示的会话保持原位（只更新内容，不移动），
            # 新出现的会话按 mtime 倒序插到列表最前。
            known = set(self._order)
            fresh = [session for key, session in by_key.items() if key not in known]
            fresh.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
            self._order = [session_key(session) for session in fresh] + [
                key for key in self._order if key in by_key
            ]
            for session in by_key.values():
                key = session_key(session)
                # annotate 没匹配上时，用本进程的内嵌托管记录兜底（见 __init__ 注释）；
                # 托管会话已死则清掉记录，让状态回到真实的「已结束」
                if "keepalive_name" not in session:
                    hosted_name = self.hosted.get(key)
                    if hosted_name:
                        if embed.is_alive(hosted_name):
                            session["keepalive_name"] = hosted_name
                        else:
                            self.hosted.pop(key, None)
                title, _ = titles.resolve_initial_title(session, self.cache)
                self.display_titles[key] = title
                # 没有可用缓存标题（纯临时兜底）才打转圈圈，等待后台进程产出。
                if not titles.has_usable_cached_title(session, self.cache):
                    self.generating.add(key)
            self._projects = None

    def projects(self) -> list[dict]:
        """跨所有来源聚合的项目文件夹列表（侧边栏用），惰性计算并缓存。"""
        with self.lock:
            if self._projects is None:
                self._projects = _project_groups(self.sessions)
            return self._projects

    def all_sessions(self) -> list[dict]:
        """返回稳定展示顺序的会话快照：已有会话位置固定不变，新出现的会话排在最前。"""
        with self.lock:
            by_key = {
                session_key(session): session
                for bucket in self.sessions.values()
                for session in bucket
            }
            ordered = [by_key[key] for key in self._order if key in by_key]
            if len(ordered) != len(by_key):
                # 兜底：_order 尚未覆盖的 key（如测试直接塞 sessions 未经合并），
                # 按 mtime 倒序排在最前，与「新会话置顶」语义一致。
                missing = [s for key, s in by_key.items() if key not in set(self._order)]
                missing.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
                ordered = missing + ordered
            return ordered

    def poll_cache_updates(self) -> None:
        """缓存文件被后台生成进程更新时重读，把新标题刷到界面并停掉对应转圈圈。"""
        mtime = self._cache_file_mtime()
        if mtime == self._cache_mtime:
            return
        self._cache_mtime = mtime
        cache = titles.load_cache()
        changed = False
        with self.lock:
            self.cache = cache
            for bucket in self.sessions.values():
                for session in bucket:
                    key = session_key(session)
                    if key not in self.generating:
                        continue
                    if titles.has_usable_cached_title(session, cache):
                        title, _ = titles.resolve_initial_title(session, cache)
                        self.display_titles[key] = title
                        self.generating.discard(key)
                        changed = True
        if changed:
            self.dirty.set()

    def snapshot(self) -> tuple[dict[str, str], set[str]]:
        """一次性取「当前展示标题」和「正在生成的 ID 集合」快照，保证两者一致。"""
        with self.lock:
            return dict(self.display_titles), set(self.generating)

    def get_title(self, session: dict) -> str:
        with self.lock:
            return self.display_titles.get(session_key(session), session["fallback_title"])

    def get_conversation(self, session: dict) -> list[ConversationMessage]:
        """按需读取并缓存选中会话的真实聊天记录；历史文件 mtime 变化（有新写入）时自动
        重读，供预览页关闭重开和停留期间的轮询刷新使用。"""
        key = session_key(session)
        path = str(session.get("path") or "")
        try:
            mtime = os.stat(path).st_mtime if path else None
        except OSError:
            mtime = None
        with self.lock:
            cached = self.conversations.get(key)
            if cached is not None and cached[0] == mtime:
                return list(cached[1])
        runtime = self.registry.get(str(session.get("source") or ""))
        messages = runtime.load_conversation(session)
        with self.lock:
            self.conversations[key] = (mtime, list(messages))
        return messages


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


def _log_embed_error(where: str, exc: BaseException) -> None:
    """内嵌后台线程的异常记录：TUI 接管终端期间 stderr 不可见，写文件留证（截断防涨爆）。"""
    try:
        path = os.path.join(os.path.dirname(titles.CACHE_FILE), "embed-error.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
            os.truncate(path, 0)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} [{where}] "
                     f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
    except OSError:
        pass  # 日志写不进去就放弃，绝不能让日志本身把后台线程弄死


def _new_session_cwd(store: SessionStore, nav, session: dict | None) -> str | None:
    """新建会话优先沿用当前项目筛选，否则使用所选会话的工作目录。

    `nav` 只需要有 `project_key` 属性（界面层的 `ui.nav.NavState`），这里不直接
    依赖 ui 包的具体类型，避免 pickup.py ↔ ui 包出现循环 import。
    """
    if nav.project_key is not None:
        return nav.project_key or None
    if session is not None:
        cwd_key = _normalize_cwd(session.get("cwd"))
        return cwd_key or None
    return None


@dataclass(frozen=True)
class _DirectLaunch:
    """直启子命令（`pickup claude …`）带进 TUI 的待托管启动计划：

    进入主循环前就把新会话托管进保活 socket 并聚焦右栏，让直启与界面内
    「新建会话」走完全相同的内嵌路径。
    """

    plan: LaunchPlan
    runtime_id: str
    ident: str


def _launch(request: LaunchRequest | NewSessionRequest, registry: RuntimeRegistry, keepalive_on: bool) -> None:
    """生成启动计划并让目标运行时接管当前终端。

    会话已经在后台保活时直接接回现场，不重新拉起一个和它竞争同一份会话文件的
    新进程；否则按 keepalive_on 开关决定新启动的进程要不要包进保活层。
    """
    if isinstance(request, LaunchRequest):
        attach = keepalive.attach_plan(request.session)
        if attach is not None:
            execute_launch(attach)
            return
        plan = registry.build_launch_plan(request)
        same_runtime = request.session.get("source") == request.target_runtime_id
        ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
    else:
        plan = registry.build_new_session_plan(request)
        ident = keepalive.new_session_ident()

    if keepalive_on:
        plan = keepalive.wrap_plan(plan, request.target_runtime_id, ident)
    execute_launch(plan)


def _format_resume_command(argv: tuple[str, ...]) -> str:
    """把启动计划的 argv 拼成可直接在 shell 中运行的命令字符串。"""
    parts = []
    for arg in argv:
        if " " in arg or "\n" in arg or '"' in arg:
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _output_json(registry, limit: int) -> None:
    """以 JSON 格式输出所有运行时的会话列表，每条附上恢复命令，然后退出。

    供大模型或自动化脚本调用：不启动 TUI，不触发后台标题生成，
    不消耗 Claude 额度。标题使用本地临时兜底标题（fallback_title）。
    """
    scanned = registry.scan_all(limit)
    result = []
    for runtime in registry:
        for session in scanned.get(runtime.id, []):
            try:
                plan = runtime.build_resume_plan(session)
                resume_cmd = _format_resume_command(plan.argv)
            except Exception:
                resume_cmd = None
            result.append({
                "runtime": session.get("source"),
                "id": session.get("id"),
                "title": session.get("fallback_title") or session.get("native_title") or "",
                "cwd": session.get("cwd") or "",
                "time": session.get("display_time") or "",
                "mtime": session.get("mtime"),
                "size_kb": round(session.get("size_kb") or 0, 1),
                "status": session.get("status_tag") or "",
                "resume_command": resume_cmd,
                "history_path": session.get("path") or "",
            })
    print(json.dumps(result, ensure_ascii=False, indent=2))


_TITLE_LOCK_FILE = os.path.join(titles.CACHE_DIR, "titles.lock")


def _run_title_daemon(registry: RuntimeRegistry, limit: int) -> None:
    """脱离 TUI 的独立标题生成进程入口（pickup --generate-titles）。

    用文件锁保证全机单实例：拿不到锁说明已有后台进程在跑，直接退出，
    避免用户反复进 pickup 堆积多个生成进程、重复消耗模型额度。
    """
    os.makedirs(titles.CACHE_DIR, exist_ok=True)
    lock_fp = open(_TITLE_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 已有进程持锁，本次无需重复生成

    try:
        scanned = registry.scan_all(limit)
        cache = titles.load_cache()
        pending = []
        for bucket in scanned.values():
            for session in bucket:
                _, needs = titles.resolve_initial_title(session, cache)
                if needs:
                    pending.append(session)
        if pending:
            titles.refresh_titles(pending, cache)
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


def _spawn_title_daemon(limit: int) -> None:
    """以脱离当前终端的方式拉起后台标题生成进程。

    start_new_session 让子进程独立成新会话/进程组：TUI 之后无论被 execvp
    替换（原生恢复）还是退出，该进程都继续把标题生成完并写入缓存。
    """
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--generate-titles", "--limit", str(limit)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # 拉起失败仅退化为「只显示临时兜底标题」，不影响主流程


def _require_tmux() -> None:
    """pickup 的会话托管/内嵌面板/断线保活全部建立在 tmux 之上，属于硬依赖；
    缺失时明确报错并给出安装提示，不静默降级出残废功能。"""
    if shutil.which("tmux") is None:
        print(
            "pickup 需要 tmux 才能运行，请先安装"
            "（macOS: brew install tmux；Debian/Ubuntu: sudo apt install tmux）。",
            file=sys.stderr,
        )
        sys.exit(1)


# 直启子命令进 TUI 侧边栏模式时的每运行时扫描深度，与主 TUI 的 --limit 默认值一致
_DIRECT_LAUNCH_LIMIT = 50


def _dispatch_direct_launch(argv: list[str], registry: RuntimeRegistry) -> None:
    """处理 `pickup [--no-keepalive] <runtime> [参数…]` 直启透传子命令。

    参数原样交给底层运行时（`registry.build_passthrough_plan` 只垫上默认全自动放行参数，
    用户已显式带了就不重复）。真实终端且内嵌可用时默认进入 TUI 侧边栏模式：新会话
    托管进保活 socket 并嵌入右栏，与界面内「新建会话」同一条路径；非真实终端、
    `--no-keepalive` 或内嵌不可用（无 tmux/被环境变量禁用）时保持旧的直接启动行为。
    """
    _require_tmux()
    no_keepalive = argv and argv[0] == "--no-keepalive"
    rest = argv[1:] if no_keepalive else argv
    runtime_id, user_args = rest[0], rest[1:]
    plan = registry.build_passthrough_plan(runtime_id, user_args)
    ident = keepalive.new_session_ident()

    if not (sys.stdin.isatty() and sys.stdout.isatty()) or not embed.available(no_keepalive):
        if keepalive.enabled(no_keepalive):
            plan = keepalive.wrap_plan(plan, runtime_id, ident)
        try:
            execute_launch(plan)
        except LaunchError as exc:
            print(f"启动失败：{exc}", file=sys.stderr)
            sys.exit(1)
        return

    keepalive.reap_idle()  # 顺带回收空闲太久没人管的后台保活会话，与主 TUI 入口一致
    store = SessionStore(limit=_DIRECT_LAUNCH_LIMIT, registry=registry)
    store.load()
    _spawn_title_daemon(_DIRECT_LAUNCH_LIMIT)

    # 与 main() 的 TUI 入口相同：趁 Textual 接管终端前探测外层终端前景/背景色，
    # 供内嵌面板聚焦时经 refresh-client -r 注入托管 pane
    global _OSC_REPORT
    _OSC_REPORT = _probe_osc_colours()

    from ui.app import run_app
    chosen = run_app(store, True, _DirectLaunch(plan, runtime_id, ident), _OSC_REPORT)
    # 兜底关闭内嵌控制通道，同 main() 的退出路径
    embed.close_channel()
    if chosen is None:
        return
    try:
        _launch(chosen, store.registry, keepalive.enabled(no_keepalive))
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # list/search/show/context/plan/describe 是面向 Agent 的机器可读子命令，整体转发给
    # agent_api，不与下面的 TUI/--json 旧参数共用同一个 parser。
    if len(sys.argv) > 1 and sys.argv[1] in agent_api.COMMAND_ROOT_NAMES:
        sys.exit(agent_api.dispatch(sys.argv[1:]))

    # `pickup claude …` / `pickup codex …`（可选前置 --no-keepalive）是直启透传子命令，同样整体
    # 绕开下面的 TUI/--json 旧参数 parser；分发探测此处只需运行时 ID 集合。
    _direct_launch_argv = sys.argv[1:]
    _direct_launch_probe = (
        _direct_launch_argv[1:] if _direct_launch_argv[:1] == ["--no-keepalive"] else _direct_launch_argv
    )
    if _direct_launch_probe and _direct_launch_probe[0] in default_registry().ids:
        _dispatch_direct_launch(_direct_launch_argv, default_registry())
        return

    parser = argparse.ArgumentParser(
        description=(
            "pickup：终端会话接力工具。\n"
            "列出 Claude Code / Codex / OpenCode / Kimi Code 最近的会话，选择后原生恢复或跨运行时接力。\n"
            "默认启动交互式 TUI（Textual），需要真实终端；非真实终端自动退化为 JSON。\n"
            "大模型 Agent 结构化查询请用 list/search/show/context/describe 子命令。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  pickup                 # 启动 TUI，交互式选择并接管终端\n"
            "  pickup --json          # 输出 JSON 会话列表后退出，不启动 TUI（旧格式）\n"
            "  pickup --json --limit 5  # JSON 模式，每个运行时最多 5 条\n"
            "  pickup describe        # 查看 list/search/show/context 等子命令的用法\n"
            "\n"
            "JSON 输出字段说明：\n"
            "  runtime        运行时标识（claude / codex / opencode / kimi）\n"
            "  id             会话 ID\n"
            "  title          会话标题（本地临时兜底，不调用 AI）\n"
            "  cwd            原会话工作目录\n"
            "  time           最后更新时间（人类可读）\n"
            "  mtime          最后更新时间（Unix 时间戳）\n"
            "  size_kb        历史文件大小（KB）\n"
            "  status         会话状态（已完成 / 待回复 / 已中断）\n"
            "  resume_command 恢复该会话的完整 shell 命令（可直接执行）\n"
            "  history_path   历史文件路径（Claude/Codex/Kimi 为 JSONL；OpenCode 为 SQLite 数据库）\n"
        ),
    )
    parser.add_argument("--limit", type=int, default=50, help="每个来源最多列出多少条")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="以 JSON 格式输出会话列表后退出，不启动 TUI")
    parser.add_argument("--no-keepalive", action="store_true", dest="no_keepalive",
                        help="本次启动不把会话包进后台保活（tmux），SSH 断开会话会跟着中断")
    parser.add_argument("--generate-titles", action="store_true", dest="generate_titles",
                        help=argparse.SUPPRESS)  # 内部用途：TUI 拉起的后台标题生成进程
    args = parser.parse_args()

    registry = default_registry()

    if args.generate_titles:
        _run_title_daemon(registry, args.limit)
        return

    if args.json_mode:
        _output_json(registry, args.limit)
        return

    # 没有真实终端（管道、脚本、被 Agent 直接调用）时，Textual 无法接管终端；自动
    # 退化为 JSON 列表而不是崩溃。stdin/stdout 分开检测：任一端不是真实终端都不能进 TUI。
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _output_json(registry, args.limit)
        return

    _require_tmux()

    keepalive_on = keepalive.enabled(args.no_keepalive)
    if keepalive_on:
        keepalive.reap_idle()  # 顺带回收空闲太久没人管的后台保活会话，不常驻额外进程

    store = SessionStore(limit=args.limit, registry=registry)
    store.load()

    if not any(store.sessions.values()):
        names = "、".join(runtime.display_name for runtime in store.registry)
        print(f"未找到任何 {names} 会话记录。", file=sys.stderr)
        sys.exit(1)

    # 拉起脱离终端的后台进程生成标题：用户秒退或原生恢复（execvp 替换进程）后仍继续，
    # TUI 通过轮询缓存文件拾取它逐批写入的标题。
    _spawn_title_daemon(args.limit)

    # 趁 Textual 接管终端前探测外层终端的前景/背景色（OSC 10/11）：内嵌面板聚焦时经
    # refresh-client -r 注入托管 pane，让 pane 内 agent 的深/浅主题检测拿到真实值
    global _OSC_REPORT
    _OSC_REPORT = _probe_osc_colours()
    if os.environ.get("PICKUP_DEBUG"):
        print(f"[pickup debug] 外层终端 OSC 10/11 探测: {_OSC_REPORT!r} "
              f"(tmux={'是' if os.environ.get('TMUX') else '否'}, "
              f"refresh -r 支持={'是' if embed.supports_theme_report() else '否'})",
              file=sys.stderr)

    from ui.app import run_app
    chosen = run_app(store, embed.available(args.no_keepalive), osc_report=_OSC_REPORT)
    # 兜底关闭内嵌控制通道：pane 聚焦时打开的 `tmux -C attach` 控制 client 只有
    # c 键关分栏才会关，Esc 退出/回车全屏接管等退出路径不经那条分支——不在这里统一
    # 兜底就会把孤儿控制 client 留在保活服务端上。close_channel 无通道时是空操作。
    embed.close_channel()
    if chosen is None:
        return

    try:
        _launch(chosen, store.registry, keepalive_on)
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
