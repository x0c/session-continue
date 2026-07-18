#!/usr/bin/env python3
"""pickup：终端会话接力工具。

单列表格列出已注册运行时（Claude Code / Codex / OpenCode / Kimi Code）的最近会话，左右切换来源、
上下选行。回车把会话内嵌到右半屏（托管在后台 tmux，左侧列表退化为窄栏，可多会话并行切换）；
按 e 则走经典全屏接管。跨运行时可通过高级操作交给其他运行时接力。tmux 为硬依赖。

注意：默认启动交互式终端 TUI（curses），需要真实终端，不能被自动化脚本或
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
import base64
import curses
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import traceback
import tty
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_api
import embed
import keepalive
import titles
from models import ConversationMessage, LaunchRequest, NewSessionRequest, format_message_time, session_key
from runtime import LaunchError, RuntimeRegistry, default_registry, execute_launch, usable_cwd


def _format_size(size_kb: float) -> str:
    return f"{size_kb / 1024:.2f}MB"


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


COL_GAP = "  "  # 列间固定间隔，避免相邻列贴在一起

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille 转圈圈，每帧占 1 列宽

# 颜色对编号（实际颜色在 _init_colors 中绑定，需在 curses.start_color() 之后调用）
PAIR_TAB_ACTIVE = 1    # 当前选中的来源标签
PAIR_TAB_INACTIVE = 2  # 未选中的来源标签
PAIR_DIM = 3           # 分隔线 / 帮助文字等弱化内容
PAIR_SELECTED = 4      # 选中行高亮
PAIR_DONE = 5          # 状态：已完成
PAIR_PENDING = 6       # 状态：待回复
PAIR_ABORTED = 7       # 状态：已中断
PAIR_KEY = 8           # 底部快捷键提示中的按键名


DIM_EXTRA_ATTR = curses.A_DIM  # 由 _init_colors 按终端能力覆盖，见其中说明

DEFAULT_COLORS_OK = False  # use_default_colors 是否成功；embed.PairPool 据此决定 -1 能否直接用


def _init_colors() -> None:
    """绑定颜色对；终端不支持彩色时静默跳过，退化为单色显示。"""
    global DIM_EXTRA_ATTR, DEFAULT_COLORS_OK
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
        DEFAULT_COLORS_OK = True
    except curses.error:
        bg = curses.COLOR_BLACK
        DEFAULT_COLORS_OK = False

    # 前景色不能写死 ANSI 亮色，也不能只靠 A_DIM，否则在浅色/白色背景终端上几乎
    # 不可读（用户实测反馈）。两类坑：
    #   1) 弱化文字老实现固定 COLOR_WHITE + A_DIM，白底上等于「白底写白字再调暗」。
    #   2) 强调色（标签/快捷键/用户消息）和状态色（绿/黄/红）都在使用处叠了 A_BOLD，
    #      而多数终端会把「加粗的 ANSI 0-7 前景色」升成对应亮色：暗青 #008080 在白底
    #      本来有 4.77 的对比度，一加粗升成亮青 #00ffff 就只剩 1.25，基本看不清；
    #      黄(1.07)、绿(1.37) 同理。
    # 修法统一：终端支持 256 色时，改用「xterm 256 调色板里在浅色和深色背景下对比度
    # 都够」的具体色号（都按 WCAG 对比度选过，白底/黑底均 ≥4.3）。256 色号是索引色，
    # A_BOLD 只会加粗字重、不会把色相升成刺眼亮色，从根上避开坑 2。退化到 8/16 色终端
    # 时才回退到原 ANSI 色（假定深色背景，与改动前行为一致，是这类终端能做到的最好效果）。
    if curses.COLORS >= 256:
        dim_fg = 244        # 中灰：不再叠加 A_DIM（灰本身够暗，再叠会过淡）
        DIM_EXTRA_ATTR = 0
        accent_fg = 30      # 暗青(teal #008787)：替代加粗后刺眼的亮青
        done_fg = 28        # 绿 #008700
        pending_fg = 130    # 琥珀 #af5f00：替代白底几乎不可见的黄
        aborted_fg = 160    # 红 #d70000
    else:
        # 8/16 色终端拿不到 256 调色板：bg=-1（默认色可用）时弱化文字用终端默认前景色，
        # 至少能随浅色/深色主题自适应；用不了默认色的老终端只能假定黑底、退回白字。
        # 强调/状态色回退到原 ANSI 色，和改动前行为一致。
        dim_fg = -1 if bg == -1 else curses.COLOR_WHITE
        DIM_EXTRA_ATTR = curses.A_DIM
        accent_fg = curses.COLOR_CYAN
        done_fg = curses.COLOR_GREEN
        pending_fg = curses.COLOR_YELLOW
        aborted_fg = curses.COLOR_RED

    curses.init_pair(PAIR_TAB_ACTIVE, accent_fg, bg)
    curses.init_pair(PAIR_TAB_INACTIVE, dim_fg, bg)
    curses.init_pair(PAIR_DIM, dim_fg, bg)
    # 选中条用亮青底黑字：填充块的背景色与终端主题无关，白底/深底都清晰，保持不变。
    curses.init_pair(PAIR_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(PAIR_DONE, done_fg, bg)
    curses.init_pair(PAIR_PENDING, pending_fg, bg)
    curses.init_pair(PAIR_ABORTED, aborted_fg, bg)
    curses.init_pair(PAIR_KEY, accent_fg, bg)


def _status_attr(live: bool) -> int:
    """状态列颜色：进行中（进程活着）用绿色高亮，已结束用暗色。"""
    return curses.color_pair(PAIR_DONE) if live else curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR


def _column_widths(screen_width: int) -> tuple[int, int, int, int, int, int]:
    """返回 # / 标题 / 目录 / 时间 / 大小 / 状态 的显示列宽（不含列间间隔）。"""
    col_num = 4
    col_time = 17
    col_size = 11
    col_status = 10
    gap_total = len(COL_GAP) * 5  # 6 列之间共 5 个间隔

    usable = max(20, screen_width - 1)
    remaining = max(10, usable - col_num - col_time - col_size - col_status - gap_total)

    col_dir = min(48, max(30, remaining * 2 // 5))
    if remaining - col_dir < 10:
        col_dir = max(12, remaining - 10)
    col_title = max(10, remaining - col_dir)
    return col_num, col_title, col_dir, col_time, col_size, col_status


# 侧边栏宽度上下限与隐藏阈值：终端总宽低于阈值时完全不画侧边栏，界面退化为单栏。
SIDEBAR_MIN_WIDTH = 14
SIDEBAR_MAX_WIDTH = 26
SIDEBAR_HIDE_THRESHOLD = 96

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


def _truncate_left(text: str, width: int) -> str:
    """显示宽度超限时从左侧截断，保留尾部信息（"…上级目录/名字"）。"""
    if width <= 0:
        return ""
    if _text_width(text) <= width:
        return text
    avail = width - 1  # 留 1 列给省略号
    kept: list[str] = []
    used = 0
    for ch in reversed(text):
        ch_width = _char_width(ch)
        if used + ch_width > avail:
            break
        kept.append(ch)
        used += ch_width
    return "…" + "".join(reversed(kept))


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


def _sidebar_width(projects: list[dict], screen_width: int) -> int:
    """返回侧边栏内容宽度（不含竖直分隔线）；0 表示隐藏。"""
    if screen_width < SIDEBAR_HIDE_THRESHOLD:
        return 0
    total_count = sum(p["count"] for p in projects)
    max_count = max([p["count"] for p in projects] + [total_count, 0])
    count_width = max(2, len(str(max_count)))
    labels = [p["label"] for p in projects] + ["全部项目"]
    max_label_width = max(_text_width(label) for label in labels)
    needed = max_label_width + 2 + 1 + count_width  # 前缀"▸ "(2) + 间隔(1) + 计数徽标
    return min(SIDEBAR_MAX_WIDTH, max(SIDEBAR_MIN_WIDTH, needed))


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
            for bucket in scanned.values():
                for session in bucket:
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


@dataclass
class UIState:
    """TUI 主循环状态：当前来源、会话列表光标，以及侧边栏焦点与选中项。"""

    source: str
    idx: int = 0
    top: int = 0
    focus: str = "list"  # "sidebar" | "list" | "pane"（"pane" 表示内嵌面板持有键盘）
    proj_idx: int = 0    # 0 = 全部项目（不过滤）
    sb_top: int = 0


# 内嵌面板（右侧面板显示托管 tmux 会话）的布局常量：左栏固定宽度、面板最小宽高，
# 终端小于分栏下限时不允许进入内嵌（回车退化为 execvp 全屏接管）。
EMBED_LEFT_BAND = 44
EMBED_MIN_PANE_W = 40
EMBED_MIN_HEIGHT = 10


@dataclass
class EmbedUI:
    """内嵌面板状态：右侧显示的托管 tmux 会话名、最新解析画面与面板尺寸。"""

    active: bool = False           # 分栏布局是否开启
    enabled: bool = False          # embed.available() 的一次性判定结果
    name: str | None = None        # 当前面板聚焦的 tmux 会话名（pickup-* 命名空间）
    session_key: str | None = None  # 对应的列表会话键，用于状态列"面板中"标注
    dead: bool = False             # 聚焦会话的进程已退出
    grid: list | None = None       # 最近一次解析出的单元格画面（embed.parse_screen）
    generation: int = 0            # grid 版本号：绘制段缓存与主循环跳帧都以它为准
    spans: list | None = None      # 按 generation 缓存的绘制段 [(y, x, text, attr)]
    spans_gen: int = -1
    size: tuple[int, int] = (0, 0)  # 面板当前宽高（字符），变化时同步 resize 托管会话
    cursor: tuple[int, int, bool] | None = None  # (x, y, 是否可见)
    copy_mode: bool = False        # 本地跟踪：pane 是否处于我们经滚轮发起的 copy-mode
    mouse_any: bool = False        # pane 内程序是否申请了鼠标上报（申请则滚轮直达程序）
    mouse_sgr: bool = False        # 鼠标上报是否为 SGR 1006 编码
    mouse_report: bool = True      # 全局鼠标上报开关（m 键切换；关闭后恢复终端原生框选）
    sel_anchor: tuple[int, int] | None = None  # 拖拽进行中的起点（屏幕 0-based）
    sel_start: tuple[int, int] | None = None   # 已完成选择的起点（高亮保留到下次操作）
    sel_end: tuple[int, int] | None = None     # 已完成选择的终点
    sel_zone: str | None = None    # 拖拽起点所在区域（"pane"/"list"）：选择范围钳制在
    # 起点区域内，防止从 pane 拖过中线时把左侧列表的文字一起选进来（用户实报）
    lock: threading.Lock = field(default_factory=threading.Lock)
    poke: threading.Event = field(default_factory=threading.Event)  # 输入/输出事件后立即补抓一帧


# 外层终端 OSC 10/11 应答原文（main() 启动时探测），供 _embed_focus 经 refresh-client -r
# 注入托管 pane——pane 内 agent 的深/浅主题自动检测因此拿到真实终端背景。
_OSC_REPORT: bytes | None = None


def _probe_osc_colours(timeout: float = 1.2) -> bytes | None:
    """启动时向外层终端查询前景/背景色（OSC 10/11），返回应答原文字节串。

    tmux 默认不应答 pane 内的 OSC 11 查询（实测：agent 在 pane 里查询石沉大海，
    深/浅主题检测只能瞎猜）；tmux 3.5a+ 的 refresh-client -r 允许把真实终端的
    应答转注入 pane，这里先趁 curses 接管前向用户终端要到应答原文。
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


def _log_embed_error(where: str, exc: BaseException) -> None:
    """内嵌后台线程的异常记录：curses 界面下 stderr 不可见，写文件留证（截断防涨爆）。"""
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


def _embed_list_width(full_width: int, height: int, emb: EmbedUI) -> int:
    """内嵌激活且终端够大时，左侧列表被压缩到的宽度；否则返回全宽（面板不显示，
    但已托管的会话在后台 tmux 里继续跑，终端拉大后面板自动回来）。"""
    if not emb.active:
        return full_width
    if full_width < EMBED_LEFT_BAND + EMBED_MIN_PANE_W or height < EMBED_MIN_HEIGHT:
        return full_width
    return EMBED_LEFT_BAND


def _visible_sessions(store: SessionStore, ui: UIState, sidebar_visible: bool) -> list[dict]:
    """当前来源下、经侧边栏项目过滤后的会话列表；侧边栏隐藏或选中"全部项目"时不过滤。"""
    bucket = store.sessions[ui.source]
    if not sidebar_visible or ui.proj_idx <= 0:
        return bucket
    projects = store.projects()
    if ui.proj_idx - 1 >= len(projects):
        return bucket
    cwd_key = projects[ui.proj_idx - 1]["cwd_key"]
    return _filter_sessions(bucket, cwd_key)


def _new_session_cwd(store: SessionStore, ui: UIState, session: dict | None, sidebar_visible: bool) -> str | None:
    """解析"新建空白会话"应该使用的工作目录：优先侧边栏选中的项目，否则退回光标所在会话的目录。

    侧边栏选中"全部项目"或未知目录项目、以及没有可用会话时都返回 None，
    调用方据此 beep 提示无目录上下文，不尝试拼一个不可靠的目录。
    """
    if sidebar_visible and ui.proj_idx > 0:
        projects = store.projects()
        if ui.proj_idx - 1 < len(projects):
            cwd_key = projects[ui.proj_idx - 1]["cwd_key"]
            return cwd_key or None
    if session is not None:
        cwd_key = _normalize_cwd(session.get("cwd"))
        return cwd_key or None
    return None


def _draw_sidebar(
    stdscr,
    projects: list[dict],
    proj_idx: int,
    sb_top: int,
    sb_w: int,
    height: int,
    focused: bool,
) -> None:
    """在左侧绘制项目文件夹列表：标题行、分隔线，然后按频率倒序的项目 + 计数徽标。"""
    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR
    title_attr = (curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD) if focused else dim
    stdscr.addnstr(0, 0, _fit_cell(" 项目", sb_w), sb_w, title_attr)
    stdscr.addnstr(1, 0, "─" * sb_w, sb_w, dim)

    total_count = sum(p["count"] for p in projects)
    entries = [{"label": "全部项目", "count": total_count}, *projects]

    list_height = max(1, height - 4)
    proj_idx = max(0, min(proj_idx, len(entries) - 1))
    if proj_idx < sb_top:
        sb_top = proj_idx
    elif proj_idx >= sb_top + list_height:
        sb_top = proj_idx - list_height + 1

    count_width = max(2, len(str(max((e["count"] for e in entries), default=0))))
    label_width = max(1, sb_w - count_width - 3)  # 前缀"▸ "(2) + 计数前 1 空格
    selected_attr = curses.color_pair(PAIR_SELECTED) | curses.A_BOLD
    active_filter_attr = curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD

    for row, i in enumerate(range(sb_top, min(len(entries), sb_top + list_height))):
        entry = entries[i]
        is_current = i == proj_idx
        prefix = "▸" if is_current else " "
        label = _truncate_left(entry["label"], label_width)
        line = (
            _fit_cell(f"{prefix} {label}", sb_w - count_width - 1)
            + " "
            + _fit_cell_right(str(entry["count"]), count_width)
        )
        if is_current:
            attr = selected_attr if focused else active_filter_attr
        else:
            attr = dim
        stdscr.addnstr(2 + row, 0, line, sb_w, attr)


def _draw(stdscr, store: SessionStore, ui: UIState, frame: int = 0,
          emb: EmbedUI | None = None, pool: "embed.PairPool | None" = None) -> None:
    stdscr.erase()
    height, full_width = stdscr.getmaxyx()

    # 终端太小时跳过绘制，避免 addnstr 写到边界外崩溃
    if height < 7 or full_width < 20:
        return

    # 内嵌激活且终端够大时，左侧列表被压缩到固定宽度，右侧区域交给会话面板；
    # 项目侧边栏是否出现由现有窄宽度逻辑按压缩后的宽度自行判定
    width = _embed_list_width(full_width, height, emb) if emb is not None else full_width
    pane_visible = width != full_width

    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR

    projects = store.projects()
    sb_w = _sidebar_width(projects, width)
    sidebar_focused = sb_w > 0 and ui.focus == "sidebar"
    list_focused = not sidebar_focused
    x0 = sb_w + 1 if sb_w else 0
    main_w = width - x0

    if sb_w:
        _draw_sidebar(stdscr, projects, ui.proj_idx, ui.sb_top, sb_w, height, sidebar_focused)
        for y in range(0, height - 2):
            stdscr.addnstr(y, sb_w, "│", 1, dim)

    # 顶部来源切换条由注册表动态生成，新增运行时无需修改界面逻辑
    active_attr = curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD
    inactive_attr = curses.color_pair(PAIR_TAB_INACTIVE) | DIM_EXTRA_ATTR
    x = x0
    runtimes = list(store.registry)
    for position, runtime in enumerate(runtimes):
        text = f" {runtime.display_name} ({len(store.sessions[runtime.id])}) "
        attr = active_attr if runtime.id == ui.source else inactive_attr
        stdscr.addnstr(0, x, text, max(0, width - 1 - x), attr)
        x += _text_width(text)
        if position < len(runtimes) - 1 and x < width - 1:
            stdscr.addnstr(0, x, "│", max(0, width - 1 - x), dim)
            x += 1

    if sidebar_focused:
        hint = "↑↓ 选项目  → 回列表"
    elif sb_w:
        hint = "←→ 切换来源/侧边栏"
    else:
        hint = "←/→ 切换来源"
    hint_x = max(x + 2, width - 1 - _text_width(hint))
    if hint_x < width - 1:
        stdscr.addnstr(0, hint_x, hint, max(0, width - 1 - hint_x), dim)
    stdscr.addnstr(1, x0, "─" * main_w, main_w, dim)

    sessions = _visible_sessions(store, ui, sb_w > 0)
    display_titles, generating = store.snapshot()
    spin = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]

    # 列宽分配：# / 标题 / 目录 / 时间 / 大小 / 状态。
    # 内嵌分栏的窄栏（compact）改走卡片式多行布局：序号/目录/大小列全部让位，
    # 标题独占一行，状态+时间放第二行——多会话并行时这是最不能丢的信息
    compact = pane_visible
    col_num, col_title, col_dir, col_time, col_size, col_status = _column_widths(main_w)

    if compact:
        header = _fit_cell(" 标题", main_w)
    else:
        header = COL_GAP.join((
            _fit_cell("#", col_num),
            _fit_cell("标题", col_title),
            _fit_cell("目录", col_dir),
            _fit_cell("时间", col_time),
            _fit_cell_right("大小", col_size),
            _fit_cell("状态", col_status),
        ))
    stdscr.addnstr(2, x0, header, main_w, dim | curses.A_BOLD)
    stdscr.addnstr(3, x0, "─" * main_w, main_w, dim)

    per_session_rows = 2 if compact else 1
    list_height = max(1, (height - 6) // per_session_rows)  # 顶部4行 + 底部分隔1行 + 帮助1行
    idx, top = ui.idx, ui.top
    if not sessions:
        message = "(该项目在当前来源没有会话，Tab 切换来源查看)" if sb_w and ui.proj_idx > 0 else "(无会话)"
        stdscr.addnstr(4, x0 + 2, message, max(0, main_w - 3), dim)
    else:
        if idx < top:
            top = idx
        elif idx >= top + list_height:
            top = idx - list_height + 1

        spinner_attr = curses.color_pair(PAIR_PENDING) | curses.A_BOLD

        for row, i in enumerate(range(top, min(len(sessions), top + list_height))):
            s = sessions[i]
            key = session_key(s)
            title = display_titles.get(key, s["fallback_title"])
            current = i == idx
            selected = current and list_focused  # 只有列表持有焦点时才用反白
            is_gen = key in generating
            is_keepalive = bool(s.get("keepalive_name"))
            status_text = "后台运行中" if is_keepalive else ("进行中" if s["live"] else "已结束")
            if emb is not None and emb.active and not emb.dead and key == emb.session_key:
                status_text = "面板中"

            if selected:
                base_attr = curses.color_pair(PAIR_SELECTED) | curses.A_BOLD
            elif current:
                base_attr = curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD
            else:
                base_attr = curses.A_NORMAL
            status_attr = base_attr if current else _status_attr(s["live"] or is_keepalive)

            y = 4 + row * per_session_rows

            if compact:
                # 卡片式：第一行 ▸+标题（生成中拆转圈圈），第二行 状态 · 相对时间；
                # _fit_cell 按宽度补齐空格，选中行两行都铺满高亮底色
                prefix = "▸ " if current else "  "
                stdscr.addnstr(y, x0, prefix, 2, base_attr if current else curses.A_NORMAL)
                tx = x0 + 2
                if is_gen:
                    stdscr.addnstr(y, tx, spin + " ", 2, base_attr if current else spinner_attr)
                    tx += 2
                    title_attr = base_attr if current else dim
                else:
                    title_attr = base_attr if current else curses.A_NORMAL
                title_cell = _fit_cell(title, main_w - (tx - x0))
                stdscr.addnstr(y, tx, title_cell, main_w - (tx - x0), title_attr)
                status_line = _fit_cell(
                    f"  {status_text} · {_format_relative_time(s['mtime'])}", main_w,
                )
                stdscr.addnstr(y + 1, x0, status_line, main_w, base_attr if current else status_attr)
                continue

            prefix = "▸" if current else " "
            num = _fit_cell(f"{prefix}{i + 1}", col_num)
            dir_col = _fit_cell(s["cwd_display"], col_dir)
            time_col = _fit_cell(_format_relative_time(s["mtime"]), col_time)
            size_col = _fit_cell_right(_format_size(s["size_kb"]), col_size)
            status_col = _fit_cell(status_text, col_status)

            x = x0

            # 标题列：生成中时拆成「转圈圈(2列宽) + 暗色临时标题」
            if is_gen:
                spin_cell = _fit_cell(spin, 2)  # 转圈圈字符 + 1 空格
                title_cell = _fit_cell(title, col_title - 2)
                spin_render_attr = base_attr if current else spinner_attr
                title_render_attr = base_attr if current else dim
                title_segments: list[tuple[str, int]] = [
                    (num, base_attr),
                    (COL_GAP, base_attr),
                    (spin_cell, spin_render_attr),
                    (title_cell, title_render_attr),
                ]
            else:
                title_col = _fit_cell(title, col_title)
                title_segments = [
                    (num, base_attr),
                    (COL_GAP, base_attr),
                    (title_col, base_attr),
                ]

            segments: list[tuple[str, int]] = list(title_segments)
            segments += [
                (COL_GAP, base_attr),
                (dir_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (time_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (size_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (status_col, status_attr),
            ]
            for cell, attr in segments:
                cell_width = max(0, width - 1 - x)
                if cell_width <= 0:
                    break
                stdscr.addnstr(y, x, cell, cell_width, attr)
                x += _text_width(cell)
            if selected and x < width - 1:
                stdscr.addnstr(y, x, " " * (width - 1 - x), width - 1 - x, base_attr)

    try:
        stdscr.addnstr(height - 2, 0, "─" * (width - 1), width - 1, dim)
        key_attr = curses.color_pair(PAIR_KEY) | curses.A_BOLD
        x = 0
        current_keepalive = bool(sessions) and 0 <= idx < len(sessions) and bool(sessions[idx].get("keepalive_name"))
        embed_on = emb is not None and emb.enabled
        if emb is not None and emb.active:
            enter_label = " 聚焦   " if current_keepalive else " 打开   "
        else:
            enter_label = " 接回   " if current_keepalive else (" 打开   " if embed_on else " 原生恢复   ")
        if sidebar_focused:
            help_entries = (
                ("↑↓", " 选项目   "),
                ("→", " 回列表   "),
                ("n", " 新建   "),
                ("Tab", " 切来源   "),
                ("q", " 退出"),
            )
        elif emb is not None and emb.active:
            # 分栏布局下左栏只有 ~44 列，放不下的条目由面板提示行补充
            help_entries = (
                ("↑↓", " 选择   "),
                ("Enter", enter_label),
                ("e", " 全屏   "),
                ("c", " 关面板   "),
                ("Space", " 预览   "),
                ("a", " 高级   "),
                ("n", " 新建   "),
                *((("x", " 关闭后台   "),) if current_keepalive else ()),
                ("q", " 退出"),
            )
        elif sb_w:
            help_entries = (
                ("↑↓", " 选择   "),
                ("←→", " 切换来源/侧边栏   "),
                ("Tab", " 切来源   "),
                ("Space", " 预览   "),
                ("Enter", enter_label),
                ("a", " 高级操作   "),
                ("n", " 新建   "),
                *((("e", " 全屏   "),) if embed_on else ()),
                *((("x", " 关闭后台   "),) if current_keepalive else ()),
                ("m", " 鼠标:开   " if (emb is None or emb.mouse_report) else " 鼠标:关   "),
                ("q", " 退出"),
            )
        else:
            help_entries = (
                ("↑↓", " 选择   "),
                ("←→/Tab", " 切换来源   "),
                ("Space", " 预览   "),
                ("Enter", enter_label),
                ("a", " 高级操作   "),
                ("n", " 新建   "),
                *((("e", " 全屏   "),) if embed_on else ()),
                *((("x", " 关闭后台   "),) if current_keepalive else ()),
                ("m", " 鼠标:开   " if (emb is None or emb.mouse_report) else " 鼠标:关   "),
                ("q", " 退出"),
            )
        for keys, label in help_entries:
            cell_width = max(0, width - 2 - x)
            if cell_width <= 0:
                break
            stdscr.addnstr(height - 1, x, keys, cell_width, key_attr)
            x += _text_width(keys)
            cell_width = max(0, width - 2 - x)
            if cell_width <= 0:
                break
            stdscr.addnstr(height - 1, x, label, cell_width, dim)
            x += _text_width(label)
    except curses.error:
        pass  # 边界写入失败时忽略，不崩溃

    if pane_visible:
        _draw_embed_pane(stdscr, emb, pool, ui, width, full_width, height)

    # 拖拽选词高亮：流式区域（首行起点→行尾、中间整行、末行→终点）反显；
    # chgat 只改属性不动文本，每帧随选择状态重画
    if emb is not None and emb.sel_start is not None and emb.sel_end is not None:
        (sx, sy), (ex, ey) = emb.sel_start, emb.sel_end
        if (sy, sx) > (ey, ex):
            sx, sy, ex, ey = ex, ey, sx, sy
        for y in range(max(0, sy), min(ey, height - 1) + 1):
            x0 = sx if y == sy else 0
            x1 = ex if y == ey else full_width - 1
            try:
                stdscr.chgat(y, max(0, x0), max(1, min(x1, full_width - 1) - max(0, x0) + 1),
                             curses.A_REVERSE)
            except curses.error:
                pass

    # 面板聚焦时把硬件光标锚定到面板内：有 agent 光标就精确跟随（输入法预览框
    # 会浮在 agent 输入框处），还没抓到光标也锚到面板左上角——总之不能停在最后
    # 绘制的底部帮助行，否则终端输入法的预编辑窗口会出现在屏幕最下面。
    # 光标可见性跟随 agent 的 cursor_flag；即使不可见，ncurses refresh 仍会更新
    # 硬件光标位置寄存器，输入法预览定位不受影响。
    cursor_shown = False
    if pane_visible and ui.focus == "pane":
        with emb.lock:
            cursor = emb.cursor
        if cursor is not None:
            cx, cy, visible = cursor
        else:
            cx, cy, visible = 0, 0, False
        pane_x0 = width + 1
        cy = max(0, min(cy, height - 3))
        cx = max(0, min(cx, full_width - 2 - pane_x0))
        try:
            curses.curs_set(1 if visible else 0)
            stdscr.move(cy, pane_x0 + cx)
            cursor_shown = True
        except curses.error:
            pass
    if not cursor_shown:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    stdscr.refresh()


def _grid_to_spans(grid: list, pane_w: int, pool: "embed.PairPool") -> list[tuple[int, int, str, int]]:
    """把单元格网格编译成 (y, x, text, attr) 绘制段；跳过纯空白默认底色的区段。

    每个画面 generation 只编译一次（池化 attr 逐格查询是全流程最贵的逐格操作），
    之后每帧原样重放 addnstr，物理重绘去重由 ncurses refresh() 的内部 diff 负责。
    """
    blank_attr = pool.attr(embed.Cell())
    spans: list[tuple[int, int, str, int]] = []
    for y, row in enumerate(grid):
        x = 0
        limit = min(len(row), pane_w)
        while x < limit:
            if row[x].wide_cont:
                x += 1
                continue
            attr = pool.attr(row[x])
            cx = x
            chars = []
            while cx < limit and pool.attr(row[cx]) == attr:
                if not row[cx].wide_cont:
                    chars.append(row[cx].ch)
                cx += 1
            text = "".join(chars)
            if text and (attr != blank_attr or text.strip()):
                spans.append((y, x, text, attr))
            x = cx
    return spans


def _draw_embed_pane(stdscr, emb: EmbedUI, pool: "embed.PairPool", ui: UIState,
                     left_w: int, full_width: int, height: int) -> None:
    """在左栏右侧绘制内嵌面板：竖分隔线、托管会话画面（按 generation 缓存重放）、底部提示行。"""
    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR
    x0 = left_w + 1
    pane_w = max(1, full_width - 1 - x0)
    pane_h = max(1, height - 2)  # 内容区 0..height-3；height-2 分隔；height-1 面板提示

    for y in range(0, height - 1):
        stdscr.addnstr(y, left_w, "│", 1, dim)

    # 面板尺寸变化：同步托管会话的 tmux 窗口大小、作废绘制段缓存并立刻补抓一帧
    if (pane_w, pane_h) != emb.size:
        emb.size = (pane_w, pane_h)
        emb.spans_gen = -1
        if emb.name and not emb.dead:
            embed.resize(emb.name, pane_w, pane_h)
        emb.poke.set()

    with emb.lock:
        grid = emb.grid
        dead = emb.dead
        name = emb.name
        generation = emb.generation
        spans = emb.spans if emb.spans_gen == generation else None

    def _center_message(text: str) -> None:
        mx = x0 + max(0, (pane_w - _text_width(text)) // 2)
        stdscr.addnstr(max(1, height // 2 - 1), mx, text, max(0, full_width - 1 - mx), dim)

    try:
        if name is None or grid is None:
            _center_message("连接中…")
        elif dead:
            _center_message("会话已结束（回车打开其他会话，c 关闭面板）")
        else:
            if spans is None:
                spans = _grid_to_spans(grid, pane_w, pool)
                with emb.lock:
                    if emb.generation == generation:
                        emb.spans = spans
                        emb.spans_gen = generation
            for y, x, text, attr in spans:
                if y >= pane_h:
                    break
                stdscr.addnstr(y, x0 + x, text, max(0, full_width - 1 - x0 - x), attr)
    except curses.error:
        pass  # 尺寸瞬变期越界写入失败时静默，下一帧自然收敛

    pane_focused = ui.focus == "pane"
    hint = "C-\\ 回列表 · 按键直接发往会话" if pane_focused else "回车 聚焦会话 · c 关闭面板"
    try:
        stdscr.addnstr(height - 2, x0, "─" * pane_w, pane_w, dim)
        hint_fitted = _fit_cell(hint, max(0, pane_w - 1)).rstrip()
        if hint_fitted:
            hint_attr = (curses.color_pair(PAIR_KEY) | curses.A_BOLD) if pane_focused else dim
            stdscr.addnstr(height - 1, x0, hint_fitted, len(hint_fitted), hint_attr)
    except curses.error:
        pass


def _draw_preview(
    stdscr,
    messages: list[ConversationMessage],
    title: str,
    runtime_name: str,
    session_id: str,
    mouse_enabled: bool,
    scroll: int,
) -> tuple[int, int]:
    """全屏绘制聊天记录，返回 (修正后的滚动位置, 当前最大滚动值)。

    调用方需要 max_scroll 判断"是否停在最底部"，以便新消息到达时决定要不要自动跟随。
    """
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 5 or width < 20:
        stdscr.refresh()
        return 0, 0

    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR
    key_attr = curses.color_pair(PAIR_KEY) | curses.A_BOLD
    user_attr = curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD
    assistant_attr = curses.color_pair(PAIR_DONE) | curses.A_BOLD
    inner_width = width - 3
    lines = _preview_lines(messages, runtime_name, inner_width)
    visible_height = height - 4
    max_scroll = max(0, len(lines) - visible_height)
    scroll = min(max(0, scroll), max_scroll)

    header = f" 对话预览 · {title} "
    stdscr.addnstr(0, 0, header, width - 1, user_attr)
    if session_id:
        # 展示完整会话 ID（而非 short_id 前缀），方便直接复制去跑 `claude --resume`/`codex resume` 等原生命令。
        id_label = f" Session ID {session_id} "
        id_x = max(0, width - 1 - _text_width(id_label))
        stdscr.addnstr(0, id_x, id_label, width - 1 - id_x, dim)
    stdscr.addnstr(1, 0, "─" * (width - 1), width - 1, dim)
    for row, (kind, line, suffix) in enumerate(lines[scroll:scroll + visible_height]):
        if kind == "user":
            attr = user_attr
        elif kind == "assistant":
            attr = assistant_attr
        elif kind == "dim":
            attr = dim
        else:
            attr = curses.A_NORMAL
        stdscr.addnstr(2 + row, 1, line, inner_width, attr)
        if suffix:
            suffix_x = 1 + _text_width(line)
            remaining = inner_width - _text_width(line)
            if remaining > 0:
                stdscr.addnstr(2 + row, suffix_x, suffix, remaining, dim)

    footer_y = height - 2
    stdscr.addnstr(footer_y, 0, "─" * (width - 1), width - 1, dim)
    mouse_hint = "m 关闭鼠标滚轮" if mouse_enabled else "m 开启鼠标滚轮（当前可框选复制）"
    hint = f"↑↓/j/k 滚动  PgUp/PgDn 翻页  Home/End 首尾  {mouse_hint}  Enter 恢复  e 全屏  a 接力  n 新建  Space/q 关闭"
    # hint 的真实显示宽度（含中文，约 100 列）常年超过终端宽度；addnstr 的第四个参数按字符数
    # 截断而不是按显示列数，宽字符会让实际写入列数超过 n。footer 又是屏幕最后一行，一旦写到
    # 最后一列会触发 ncurses 的“右下角写入”保护性异常（addnwstr() returned ERR）直接崩掉整个
    # TUI（真实终端 <100 列时必现）。这里用 _fit_cell 按显示宽度截断（留 2 列安全边界），
    # 保证实际写入列数永远算得出来、不会撞到最后一列。
    hint_fitted = _fit_cell(hint, max(0, width - 2)).rstrip()
    stdscr.addnstr(footer_y + 1, 0, hint_fitted, len(hint_fitted), key_attr)
    if max_scroll:
        progress = f"{scroll + 1}/{max_scroll + 1}"
        progress_x = max(0, width - 1 - _text_width(progress))
        stdscr.addnstr(footer_y + 1, progress_x, progress, len(progress), dim)
    stdscr.refresh()
    return scroll, max_scroll


PREVIEW_MOUSE_SCROLL_LINES = 3  # 滚轮一格滚动的行数，参照 less/vim 等终端工具的常见默认值


def _preview_mouse_scroll_delta() -> int | None:
    """读取一次鼠标事件，返回滚轮方向对应的滚动行数增量；不是滚轮事件或读取失败时返回 None。"""
    try:
        _, _, _, _, bstate = curses.getmouse()
    except curses.error:
        return None
    if bstate & curses.BUTTON4_PRESSED:
        return -PREVIEW_MOUSE_SCROLL_LINES
    if bstate & getattr(curses, "BUTTON5_PRESSED", 0):
        return PREVIEW_MOUSE_SCROLL_LINES
    return None


def _apply_mousemask(enabled: bool) -> None:
    """开关鼠标上报；关闭后终端恢复原生框选/复制。

    订阅集包含滚轮与左键按下/抬起/拖动：这些事件必须先订阅到，`_pane_mouse`
    才能拿到并按区域路由（转发滚轮 / 拖拽选词 / 丢弃其余）；未订阅的鼠标序列
    会被 ncurses 整个吞掉（实测确认不会漏进键盘通道变成垃圾按键），那拖拽/点击
    事件就连「快速丢弃」的机会都没有，全卡在队列里。REPORT_MOUSE_POSITION 让
    拖拽过程（motion）也可达，选词高亮得以实时跟随。
    """
    mask = 0
    if enabled:
        mask = (curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED
                | getattr(curses, "BUTTON1_POSITION_CHANGED", 0)
                | getattr(curses, "REPORT_MOUSE_POSITION", 0)
                | curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0))
    try:
        curses.mousemask(mask)
    except curses.error:
        pass  # 终端或 curses 编译版本不支持鼠标上报时静默降级为纯键盘操作


def _show_preview(
    stdscr,
    store: SessionStore,
    ui: UIState,
    session: dict,
    title: str,
    sidebar_visible: bool,
    mouse_report: bool = True,
) -> tuple[LaunchRequest | NewSessionRequest, bool] | None:
    """打开全屏聊天记录；回车恢复（内嵌可用时内嵌打开），e 强制全屏接管，空格或 q 关闭，
    `a`/`n` 等会话级快捷键与列表页一致。

    返回值是 (启动请求, 是否强制全屏) 二元组，调用方据此决定内嵌打开还是交给
    main() 走 execvp；None 表示用户只关闭了预览、不产生启动。

    默认开启鼠标滚轮上报，按 `m` 可临时关闭以用回终端原生的鼠标框选/复制（开启鼠标
    上报期间，终端会把所有鼠标事件——包括拖拽选中——都发给本程序，原生框选会失效，
    这是终端鼠标协议的固有限制，不是可以只订阅滚轮事件就绕开的）。退出预览页（含所有
    提前 return 路径）恢复主循环的滚轮上报——主列表页/内嵌面板的滚轮同样依赖它。

    实时刷新：`_run` 已把 stdscr 设为 200ms 超时非阻塞 getch，这里复用同一节奏——每约
    1 秒检查一次会话历史文件是否有新写入（`store.get_conversation` 内部按 mtime 判断，
    没变化就是一次 os.stat，开销可忽略）。只有停留在最底部（正在追最新进展）才自动
    跟随新消息滚到底部；已经往上翻阅历史时保持当前位置不动，不打扰阅读。
    """
    messages = store.get_conversation(session)
    runtime_name = store.registry.get(str(session.get("source") or "")).display_name
    session_id = str(session.get("id") or "")
    scroll = 10 ** 9  # 聊天预览默认定位到最近一轮
    max_scroll = 0
    mouse_enabled = mouse_report
    _apply_mousemask(mouse_enabled)
    frame = 0
    POLL_EVERY = 5  # 200ms * 5 ≈ 1s，与主列表页的标题缓存轮询同频
    redraw = True
    try:
        while True:
            if redraw:
                scroll, max_scroll = _draw_preview(
                    stdscr, messages, title, runtime_name, session_id, mouse_enabled, scroll,
                )
            redraw = True  # 下一帧默认恢复绘制；鼠标非滚轮事件会把它按掉
            try:
                ch = stdscr.getch()
            except curses.error:
                continue
            if ch == -1:
                frame += 1
                if frame % POLL_EVERY == 0:
                    at_bottom = scroll >= max_scroll
                    messages = store.get_conversation(session)
                    if at_bottom:
                        scroll = 10 ** 9  # 停在底部时自动跟随新消息
                continue
            if ch in (ord(" "), ord("q")):
                stdscr.clear()
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                stdscr.clear()
                return LaunchRequest(session, ui.source, title), False
            if ch == ord("e"):
                stdscr.clear()
                return LaunchRequest(session, ui.source, title), True
            if ch in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif ch == curses.KEY_PPAGE:
                scroll = max(0, scroll - 10)
            elif ch == curses.KEY_NPAGE:
                scroll += 10
            elif ch == curses.KEY_HOME:
                scroll = 0
            elif ch == curses.KEY_END:
                scroll = 10 ** 9
            elif ch == curses.KEY_MOUSE:
                delta = _preview_mouse_scroll_delta()
                if delta is not None:
                    scroll = max(0, scroll + delta)
                else:
                    redraw = False  # 拖拽/点击事件流：不滚动也不整帧重绘（防重绘风暴）
            elif ch == ord("m"):
                mouse_enabled = not mouse_enabled
                _apply_mousemask(mouse_enabled)
            else:
                result = _session_action(ch, stdscr, store, ui, session, sidebar_visible)
                if result is not _ACTION_STAY and result is not _ACTION_PASS:
                    stdscr.clear()
                    return result, False
    finally:
        _apply_mousemask(mouse_report)  # 恢复主循环的全局鼠标状态，预览页内的 m 切换不带走


def _draw_runtime_menu(stdscr, store: SessionStore, title: str, action_for, selected: int) -> None:
    """在主列表之上绘制运行时选择弹窗。action_for(runtime) 返回每一项的说明文案。"""
    height, width = stdscr.getmaxyx()
    runtimes = list(store.registry)
    if height < len(runtimes) + 7 or width < 44:
        return

    box_width = min(76, width - 4)
    box_height = len(runtimes) + 5
    left = (width - box_width) // 2
    top = (height - box_height) // 2
    normal = curses.color_pair(PAIR_DIM) | curses.A_BOLD
    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR
    selected_attr = curses.color_pair(PAIR_SELECTED) | curses.A_BOLD

    stdscr.addnstr(top, left, "┌" + "─" * (box_width - 2) + "┐", box_width, normal)
    title_text = f" {title} "
    stdscr.addnstr(top, left + max(1, (box_width - _text_width(title_text)) // 2), title_text, box_width - 2, normal)
    for row in range(1, box_height - 1):
        stdscr.addnstr(top + row, left, "│" + " " * (box_width - 2) + "│", box_width, normal)
    stdscr.addnstr(top + box_height - 1, left, "└" + "─" * (box_width - 2) + "┘", box_width, normal)

    for index, runtime in enumerate(runtimes):
        available = runtime.is_available()
        action = action_for(runtime)
        if not available:
            action += "［未安装］"
        prefix = "▸" if index == selected else " "
        line = _fit_cell(f"{prefix} {runtime.display_name:<10} {action}", box_width - 4)
        attr = selected_attr if index == selected else (normal if available else dim)
        stdscr.addnstr(top + 2 + index, left + 2, line, box_width - 4, attr)

    hint = "↑↓ 选择   Enter 确认   q 返回"
    stdscr.addnstr(top + box_height - 2, left + 2, hint, box_width - 4, dim)
    stdscr.refresh()


def _pick_runtime(stdscr, store: SessionStore, title: str, action_for, default_index: int) -> str | None:
    """通用运行时选择弹窗：↑↓ 选择、Enter 确认（未安装则 beep 拒绝）、q 取消。"""
    runtimes = list(store.registry)
    selected = default_index

    while True:
        _draw_runtime_menu(stdscr, store, title, action_for, selected)
        try:
            ch = stdscr.getch()
        except curses.error:
            continue
        if ch == -1:
            continue
        if ch == ord("q"):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(runtimes)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(runtimes)
        elif ch in (10, 13, curses.KEY_ENTER):
            runtime = runtimes[selected]
            if runtime.is_available():
                return runtime.id
            curses.beep()


def _choose_target_runtime(stdscr, store: SessionStore, source: str) -> str | None:
    """打开高级操作菜单；默认选中第一个可用的其他运行时。"""
    runtimes = list(store.registry)
    source_name = store.registry.get(source).display_name

    def action_for(runtime) -> str:
        if runtime.id == source:
            return "原生恢复（保留完整上下文）"
        return f"读取 {source_name} 历史后新建会话"

    default_index = next(
        (i for i, runtime in enumerate(runtimes) if runtime.id != source and runtime.is_available()),
        next((i for i, runtime in enumerate(runtimes) if runtime.id == source), 0),
    )
    return _pick_runtime(stdscr, store, "高级操作：选择接力运行时", action_for, default_index)


def _pick_runtime_for_new_session(stdscr, store: SessionStore, default_id: str) -> str | None:
    """新建会话菜单；默认高亮 default_id（当前所在标签的运行时）对应项。"""
    runtimes = list(store.registry)

    def action_for(_runtime) -> str:
        return "在该目录下新建空白会话"

    default_index = next(
        (i for i, runtime in enumerate(runtimes) if runtime.id == default_id and runtime.is_available()),
        next((i for i, runtime in enumerate(runtimes) if runtime.is_available()), 0),
    )
    return _pick_runtime(stdscr, store, "新建会话：选择运行时", action_for, default_index)


_ACTION_STAY = object()  # 按键已被处理（弹窗被取消 / beep 拒绝），调用方留在当前视图重绘
_ACTION_PASS = object()  # 不是会话级动作键，调用方自行处理（导航、滚动等）


def _confirm_kill_keepalive(stdscr, label: str) -> bool:
    """关闭后台保活进程前的一次性确认；按 y 确认，其余任意键取消。"""
    height, width = stdscr.getmaxyx()
    message = f"关闭后台进程「{label}」？未保存的当前任务进度将丢失"
    box_width = min(width - 4, max(30, _text_width(message) + 6))
    if height < 6 or box_width < 20:
        return False
    box_height = 4
    left = (width - box_width) // 2
    top = (height - box_height) // 2
    normal = curses.color_pair(PAIR_DIM) | curses.A_BOLD
    dim = curses.color_pair(PAIR_DIM) | DIM_EXTRA_ATTR

    stdscr.addnstr(top, left, "┌" + "─" * (box_width - 2) + "┐", box_width, normal)
    stdscr.addnstr(top + 1, left, "│" + " " * (box_width - 2) + "│", box_width, normal)
    stdscr.addnstr(top + 1, left + 2, _fit_cell(message, box_width - 4), box_width - 4, normal)
    stdscr.addnstr(top + 2, left, "│" + " " * (box_width - 2) + "│", box_width, normal)
    stdscr.addnstr(top + 2, left + 2, "y 确认关闭   其他键取消", box_width - 4, dim)
    stdscr.addnstr(top + 3, left, "└" + "─" * (box_width - 2) + "┘", box_width, normal)
    stdscr.refresh()

    # stdscr 处于 200ms 非阻塞超时模式（_run 里 stdscr.timeout(200)）；getch() 超时
    # 返回 -1 必须继续等待用户真正按键，不能当成"其他键取消"——否则确认框会在
    # 200ms 后自动消失，用户实际上没有机会按 y 确认（真实故障，已复现）。
    while True:
        try:
            ch = stdscr.getch()
        except curses.error:
            continue
        if ch != -1:
            return ch in (ord("y"), ord("Y"))


def _session_action(
    ch: int,
    stdscr,
    store: SessionStore,
    ui: UIState,
    session: dict | None,
    sidebar_visible: bool,
):
    """处理列表页与预览页共用的会话级快捷键：`a` 接力、`n` 新建会话、`x` 关闭后台保活。

    列表页和预览页的按键循环都要经过这里，保证同一个键在两处行为一致；
    未来新增会话级快捷键只需要在这里加一个分支，两处会自动同时支持。
    """
    if ch == ord("a"):
        if session is None:
            curses.beep()
            return _ACTION_STAY
        target = _choose_target_runtime(stdscr, store, ui.source)
        if target is None:
            return _ACTION_STAY
        return LaunchRequest(session, target, store.get_title(session))
    if ch == ord("x"):
        keepalive_name = session.get("keepalive_name") if session else None
        if not keepalive_name:
            curses.beep()
            return _ACTION_STAY
        if _confirm_kill_keepalive(stdscr, store.get_title(session)):
            keepalive.kill(keepalive_name)
            session.pop("keepalive_name", None)
            store.hosted.pop(session_key(session), None)
        stdscr.clear()
        return _ACTION_STAY
    if ch == ord("n"):
        cwd = usable_cwd(_new_session_cwd(store, ui, session, sidebar_visible))
        if cwd is None:
            curses.beep()
            return _ACTION_STAY
        if ui.focus == "sidebar":
            target = _pick_runtime_for_new_session(stdscr, store, ui.source)
            if target is None:
                return _ACTION_STAY
        else:
            target = ui.source
        return NewSessionRequest(target, cwd)
    return _ACTION_PASS


def _run(stdscr, store: SessionStore, embed_ok: bool) -> LaunchRequest | NewSessionRequest | None:
    curses.curs_set(0)
    _init_colors()
    # raw 而非默认 cbreak：C-\(0x1C)/C-c 必须作为普通按键读入——pane 聚焦时
    # C-c/C-z 等控制键要原样透传给托管会话，不能让它们变成杀掉/挂起 pickup 的信号
    curses.raw()
    stdscr.keypad(True)  # 关键：没有这行，方向键的 ESC 序列不会被解码成 KEY_LEFT/RIGHT/UP/DOWN，
    # 裸 ESC(27) 会被退出键判断提前吃掉，导致方向键失灵
    stdscr.timeout(200)  # 非阻塞 getch，留出空间检测后台标题刷新
    # 开启终端 bracketed paste：pane 聚焦时粘贴内容被 \e[200~ ... \e[201~ 包住，
    # 可干净识别后经 paste buffer 一次性注入会话（main() 在 wrapper 返回后关闭该模式）
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()
    # 开启滚轮上报：pane 内滚轮驱动 copy-mode 回滚或直达申请了鼠标的程序，
    # 列表页滚轮滚动会话选择。期间终端原生框选失效（鼠标协议固有限制），
    # 主流终端可用修饰键拖拽（Option/Shift+拖拽）绕过。
    _apply_mousemask(True)

    pool = embed.PairPool(first=16, use_default=DEFAULT_COLORS_OK)
    emb = EmbedUI(enabled=embed_ok)

    runtime_ids = store.registry.ids
    source = next((runtime_id for runtime_id in runtime_ids if store.sessions[runtime_id]), runtime_ids[0])
    ui = UIState(source=source)

    def _sync_top() -> None:
        """更新 ui.top 与可见区对齐，保持会话列表状态和实际渲染同步。"""
        height, full_w = stdscr.getmaxyx()
        per = 2 if _embed_list_width(full_w, height, emb) != full_w else 1
        list_height = max(1, (height - 6) // per)
        if ui.idx < ui.top:
            ui.top = ui.idx
        elif ui.idx >= ui.top + list_height:
            ui.top = ui.idx - list_height + 1

    def _sync_sidebar_top() -> None:
        """更新 ui.sb_top 与侧边栏可见区对齐，与 _sync_top 对称。"""
        height, _ = stdscr.getmaxyx()
        list_height = max(1, height - 4)
        if ui.proj_idx < ui.sb_top:
            ui.sb_top = ui.proj_idx
        elif ui.proj_idx >= ui.sb_top + list_height:
            ui.sb_top = ui.proj_idx - list_height + 1

    # 记住"用户当前看的是哪个会话/哪个项目"，供后台重扫改变列表顺序或增删条目后，
    # 把光标和侧边栏选中项定位回原处，而不是被新列表的下标错位带偏或跳回开头。
    last_session_key: str | None = None
    last_cwd_key: str | None = None

    def _remember_selection(sidebar_visible: bool) -> None:
        nonlocal last_session_key, last_cwd_key
        sessions_now = _visible_sessions(store, ui, sidebar_visible)
        if sessions_now and 0 <= ui.idx < len(sessions_now):
            last_session_key = session_key(sessions_now[ui.idx])
        projects_now = store.projects()
        if ui.proj_idx > 0 and ui.proj_idx - 1 < len(projects_now):
            last_cwd_key = projects_now[ui.proj_idx - 1]["cwd_key"]
        else:
            last_cwd_key = None  # "全部项目" 或越界，无项目可定位

    def _relocate_after_refresh(sidebar_visible: bool) -> None:
        if last_cwd_key is not None:
            projects_now = store.projects()
            for i, project in enumerate(projects_now):
                if project["cwd_key"] == last_cwd_key:
                    ui.proj_idx = i + 1
                    break
            else:
                ui.proj_idx = min(ui.proj_idx, len(projects_now))
        if last_session_key is not None:
            sessions_now = _visible_sessions(store, ui, sidebar_visible)
            for i, session in enumerate(sessions_now):
                if session_key(session) == last_session_key:
                    ui.idx = i
                    break
            else:
                ui.idx = max(0, min(ui.idx, len(sessions_now) - 1)) if sessions_now else 0

    # getch 超时为 200ms，每 5 帧（约 1 秒）轮询一次缓存文件，拾取后台进程产出的新标题。
    POLL_EVERY = 5

    # ---- 内嵌面板机制：画面抓取、按键转发、打开/聚焦托管会话 ----

    def _capture_loop() -> None:
        """抓取聚焦托管会话的画面与交互状态；emb.poke 触发立即补帧。

        控制通道存活时事件驱动（通道把 %output 转成 poke，回显零轮询等待），辅以
        2s 慢速兜底轮询防事件丢失；通道死亡/不可用时退回 200ms 传统轮询。
        %output 风暴经 MIN_INTERVAL 限速，避免高频抓帧的 fork 风暴。
        守护线程随进程退出自然结束，与 _background_refresh 同一模式。"""
        MIN_INTERVAL = 0.04   # 事件驱动下的最小抓帧间隔（刷屏限速）
        IDLE_FALLBACK = 2.0   # 有控制通道时的兜底轮询间隔
        POLL_INTERVAL = 0.2   # 无控制通道时的传统轮询间隔
        misses = 0
        last_text: str | None = None
        last_name: str | None = None  # 聚焦会话切换检测：换会话必须重置 last_text，
        # 否则切回一个画面静止的会话时 capture 文本与 last_text 相同、跳过解析，
        # emb.grid 永远是 _embed_focus 重置的 None——面板一直停在「连接中…」
        last_capture = 0.0
        while True:
            interval = POLL_INTERVAL
            try:
                name = emb.name if emb.active else None
                if name != last_name:
                    last_name = name
                    last_text = None
                    misses = 0
                if name is not None:
                    channel = embed.active_channel(name)
                    gap = time.monotonic() - last_capture
                    if 0 < gap < MIN_INTERVAL:
                        time.sleep(MIN_INTERVAL - gap)
                    text = embed.capture(name)
                    last_capture = time.monotonic()
                    if text is None:
                        # capture 失败常常只是 tmux 瞬时超时；必须连续失败且
                        # has-session 确认不存在才判定死亡，避免焦点被误抢、状态抖动
                        misses += 1
                        if misses >= 3 and not embed.is_alive(name):
                            with emb.lock:
                                emb.dead = True
                            store.dirty.set()
                    else:
                        misses = 0
                        if text != last_text:
                            # 画面真的变了才解析/重绘：8k 单元格的解析是全流程最贵的
                            # Python 操作，agent 空闲时（无输出）整条链路到这就停
                            w, h = emb.size
                            grid = embed.parse_screen(text, w, h) if (w, h) != (0, 0) else None
                            state = embed.pane_state(name) if ui.focus == "pane" else None
                            with emb.lock:
                                if grid is not None:
                                    emb.grid = grid
                                    # last_text 只在真正解析入 grid 时记录：_embed_focus
                                    # 后 emb.size 尚未就绪的窗口期抓到文本会得 grid=None，
                                    # 若此时就标记 last_text，静止会话下一轮 capture 文本
                                    # 相同便永远跳过解析——面板卡在「连接中…」（用户实报）
                                    last_text = text
                                emb.dead = False
                                if state is not None:
                                    emb.cursor = state[:3]
                                    emb.mouse_any, emb.mouse_sgr = state[3], state[4]
                                emb.generation += 1
                            store.dirty.set()
                        elif ui.focus == "pane":
                            # 画面没变但光标/鼠标模式可能变了（方向键移动光标不回显）
                            state = embed.pane_state(name)
                            if state is not None:
                                with emb.lock:
                                    if state[:3] != emb.cursor:
                                        emb.cursor = state[:3]
                                        store.dirty.set()
                                    emb.mouse_any, emb.mouse_sgr = state[3], state[4]
                    interval = IDLE_FALLBACK if channel is not None else POLL_INTERVAL
            except Exception as exc:
                # 抓帧线程绝不能死：任何未料异常记录后继续，线程一旦静默退出，
                # 面板就永远停在「连接中…」且无任何线索
                _log_embed_error("capture-loop", exc)
                time.sleep(0.5)  # 异常风暴时限速，避免日志狂写
            emb.poke.wait(interval)
            emb.poke.clear()

    threading.Thread(target=_capture_loop, daemon=True).start()

    def _pane_size_now() -> tuple[int, int]:
        height, width = stdscr.getmaxyx()
        return max(20, width - EMBED_LEFT_BAND - 2), max(4, height - 2)

    def _embed_usable_here() -> bool:
        if not emb.enabled:
            return False
        height, width = stdscr.getmaxyx()
        return width >= EMBED_LEFT_BAND + EMBED_MIN_PANE_W and height >= EMBED_MIN_HEIGHT

    def _embed_focus(name: str, skey: str | None) -> None:
        """把面板聚焦到某个已在保活 socket 上的托管会话（不新建进程）。"""
        emb.name = name
        emb.active = True
        emb.dead = False
        emb.grid = None
        emb.cursor = None
        emb.copy_mode = False
        emb.session_key = skey
        emb.size = (0, 0)  # 触发下一帧重算尺寸并 resize 托管窗口
        ui.focus = "pane"
        # 控制通道：%output 直接转成「该抓帧了」信号；切换会话时自动关旧开新
        channel = embed.open_channel(name, on_output=emb.poke.set)
        # 终端背景色注入：此后 pane 内 agent 的 OSC 11 查询由 tmux 按真实值应答
        if channel is not None and _OSC_REPORT and embed.supports_theme_report():
            embed.report_theme(channel, _OSC_REPORT)
        emb.poke.set()

    def _embed_open(request: LaunchRequest | NewSessionRequest) -> None:
        """把启动请求内嵌打开：已托管的会话直接聚焦画面，否则先包进保活 socket 再聚焦。"""
        same_runtime = isinstance(request, LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
        )
        if isinstance(request, LaunchRequest):
            existing = request.session.get("keepalive_name") if same_runtime else None
            if existing:
                _embed_focus(existing, session_key(request.session))
                return
            plan = store.registry.build_launch_plan(request)
            ident = request.session["id"] if same_runtime else keepalive.new_session_ident()
        else:
            plan = store.registry.build_new_session_plan(request)
            ident = keepalive.new_session_ident()
        try:
            name = embed.host_session(plan, request.target_runtime_id, ident, *_pane_size_now())
        except (embed.EmbedError, LaunchError):
            curses.beep()
            return
        if same_runtime:
            # 状态列/x 键立刻生效，不等 3 秒后的后台重扫；同时记入托管表，
            # 供 annotate 匹配不上的场景兜底（见 SessionStore.__init__ 注释）
            request.session["keepalive_name"] = name
            store.hosted[session_key(request.session)] = name
        _embed_focus(name, session_key(request.session) if same_runtime else None)

    pending_input = bytearray()

    def _flush_pending() -> None:
        if pending_input and emb.name:
            embed.send_literal(emb.name, pending_input.decode("utf-8", errors="replace"))
        pending_input.clear()

    def _drain_input(first: int) -> None:
        """可打印字节（含 UTF-8 高位字节）攒批后一次 send-keys -l：快速连打/IME 提交
        时字符成串到达，排干输入缓冲攒成一批，避免每键一个 tmux 子进程。"""
        pending_input.append(first)
        stdscr.timeout(0)
        try:
            while len(pending_input) < 4096:
                nxt = stdscr.getch()
                if nxt == -1:
                    break
                if 32 <= nxt <= 255 and nxt != 127:
                    pending_input.append(nxt)
                else:
                    curses.ungetch(nxt)
                    break
        finally:
            stdscr.timeout(200)
        _flush_pending()

    def _read_paste_payload() -> None:
        """读取 bracketed paste 正文（\e[200~ 之后、\e[201~ 之前），整段注入会话。"""
        payload = bytearray()
        stdscr.timeout(1000)
        try:
            while True:
                c = stdscr.getch()
                if c == -1:
                    break  # 粘贴流异常中断：已读到的内容照常注入
                if c > 255:
                    continue  # 功能键混进粘贴流：丢弃，不破坏字节序
                payload.append(c)
                if payload[-6:] == b"\x1b[201~":
                    del payload[-6:]
                    break
        finally:
            stdscr.timeout(200)
        if emb.name:
            embed.paste(emb.name, payload.decode("utf-8", errors="replace"))

    def _forward_escape() -> None:
        """ESC 开头的输入分流：bracketed paste 标记 / 裸 Escape / Alt+键。"""
        stdscr.timeout(40)
        try:
            nxt = stdscr.getch()
            if nxt == -1:
                embed.send_key(emb.name, "Escape")
                return
            if nxt == ord("["):
                seq = bytearray()
                c = -1
                while len(seq) < 8:
                    c = stdscr.getch()
                    if c == -1 or c == ord("~"):
                        break
                    seq.append(c)
                if bytes(seq) == b"200" and c == ord("~"):
                    _read_paste_payload()
                    return
                # 其他 CSI 序列：近似补发 Escape + 已读字节（罕见路径，不追求完美）
                embed.send_key(emb.name, "Escape")
                if seq:
                    embed.send_literal(emb.name, "[" + seq.decode("ascii", errors="replace"))
                return
            # Alt+键：发 Escape 再把后随按键按正常路径转发（tmux 端等效 M-x）
            embed.send_key(emb.name, "Escape")
            if 32 <= nxt <= 255 and nxt != 127:
                _drain_input(nxt)
            else:
                translated = embed.translate_key(nxt)
                if translated is not None:
                    embed.send_key(emb.name, translated[1])
        finally:
            stdscr.timeout(200)

    def _copy_selection(start: tuple[int, int], end: tuple[int, int]) -> None:
        """把流式选择区（start→end 跨行连续）的屏幕文本经 OSC 52 写入剪贴板。

        文本直接读 stdscr 当前内容（`inchnstr`），列表/pane/侧栏任何可见区域通吃；
        OSC 52 经 SSH 透传到用户本地终端剪贴板（iTerm2/kitty/alacritty 均支持），
        是鼠标上报开启期间（终端原生框选失效）的选词通道。
        """
        (sx, sy), (ex, ey) = start, end
        if (sy, sx) > (ey, ex):
            sx, sy, ex, ey = ex, ey, sx, sy
        width = stdscr.getmaxyx()[1]
        lines: list[str] = []
        for y in range(sy, ey + 1):
            x0 = sx if y == sy else 0
            x1 = ex if y == ey else width - 1
            cells = max(1, x1 - x0 + 1)
            try:
                # instr 的 n 按**字节**截断（实测：n=8 读到「↓ 选」就停，「择」
                # 的 3 字节放不下）——按格数 ×4 过读保证宽字符完整，再按格宽截回
                chunk = stdscr.instr(y, x0, cells * 4)
            except curses.error:
                continue
            lines.append(_fit_cell(chunk.decode("utf-8", errors="replace"), cells).rstrip())
        text = "\n".join(lines).strip("\n")
        if text:
            payload = base64.b64encode(text.encode()).decode()
            sys.stdout.write(f"\x1b]52;c;{payload}\a")
            sys.stdout.flush()

    def _clamp_to_zone(mx: int, my: int) -> tuple[int, int]:
        """把拖拽终点钳制在起点所在区域内（emb.sel_zone 在 press 时锁定）：
        pane 区选词不越过分栏线进左栏，左栏选词不越进 pane；pane 不可见时不钳。"""
        if emb.sel_zone is None:
            return mx, my
        height_now, full_width = stdscr.getmaxyx()
        pane_x0 = EMBED_LEFT_BAND + 1
        pane_h = max(1, height_now - 2)
        pane_visible = _embed_list_width(full_width, height_now, emb) != full_width
        if not pane_visible:
            return mx, my
        if emb.sel_zone == "pane":
            return max(mx, pane_x0), min(my, pane_h - 1)
        return min(mx, pane_x0 - 1), my

    def _pane_mouse() -> bool:
        """鼠标事件总入口；返回 True 表示改了 UI 状态需要重绘。

        分发顺序：
        1. 左键按下/拖动/抬起 → **拖拽选词**（全屏任意区域，pickup 内置）：
           按下记起点、拖动实时更新高亮、抬起按流式区域复制到剪贴板（OSC 52）。
           这是鼠标上报开启期间（终端原生框选被程序独占）的选词通道；
           m 键关闭上报后事件不上报，原生框选自动接管，两者不冲突。
        2. 滚轮 → 区域隔离路由：pane 内且 agent 申请了鼠标 → SGR 序列直达 agent；
           否则 copy-mode 翻回滚历史；pane 外（左栏）滚会话列表。
        3. 其余（悬停、点击）快速丢弃。
        事件流只转发或丢弃、绝不触发整帧重绘（选词高亮除外）——这是拖拽
        不卡死的关键：画面更新一律由 capture/poke 驱动。
        """
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return False
        wheel_up = bool(bstate & curses.BUTTON4_PRESSED)
        wheel_down = bool(bstate & getattr(curses, "BUTTON5_PRESSED", 0))

        if bstate & curses.BUTTON1_PRESSED:
            emb.sel_anchor = (mx, my)
            emb.sel_start = None
            emb.sel_end = None
            # 起点锁定选区所在区域：pane 内起的拖不越进左栏，左栏起的拖不越进 pane
            _h, _w = stdscr.getmaxyx()
            _px0 = EMBED_LEFT_BAND + 1
            _pv = _embed_list_width(_w, _h, emb) != _w
            emb.sel_zone = "pane" if (_pv and mx >= _px0 and my < _h - 2) else "list"
            return True  # 记起点并清旧高亮
        if (bstate & getattr(curses, "REPORT_MOUSE_POSITION", 0)) and emb.sel_anchor is not None:
            emb.sel_start = emb.sel_anchor
            emb.sel_end = _clamp_to_zone(mx, my)
            return True  # 高亮实时跟随
        if bstate & curses.BUTTON1_RELEASED and emb.sel_anchor is not None:
            start = emb.sel_anchor
            emb.sel_anchor = None
            end = _clamp_to_zone(mx, my)
            if abs(end[0] - start[0]) + abs(end[1] - start[1]) >= 2:
                emb.sel_start = start
                emb.sel_end = end
                _copy_selection(start, end)
            else:
                emb.sel_start = None  # 位移过小视为点击，不产生选区
                emb.sel_end = None
            return True

        height_now, full_width = stdscr.getmaxyx()
        pane_x0 = EMBED_LEFT_BAND + 1
        pane_h = max(1, height_now - 2)
        pane_visible = _embed_list_width(full_width, height_now, emb) != full_width
        if not pane_visible or mx < pane_x0 or my >= pane_h:
            if wheel_up or wheel_down:
                # 滚轮跟焦点走：侧栏焦点滚项目、其余焦点滚会话列表
                delta = -PREVIEW_MOUSE_SCROLL_LINES if wheel_up else PREVIEW_MOUSE_SCROLL_LINES
                if ui.focus == "sidebar":
                    ui.proj_idx = max(0, min(ui.proj_idx + delta, len(store.projects())))
                    return True
                sessions_now = _visible_sessions(store, ui, _sidebar_width(store.projects(), EMBED_LEFT_BAND) > 0)
                if sessions_now:
                    ui.idx = max(0, min(ui.idx + delta, len(sessions_now) - 1))
                    return True
            return False
        if not emb.name or emb.dead:
            return False
        if emb.mouse_any and emb.mouse_sgr:
            if wheel_up or wheel_down:
                # 只转发滚轮：单事件无配对，协议行为稳定。点击/拖拽不转发——
                # ncurses 会把快速连续的 press+release 合并成 CLICK（未订阅即整个
                # 丢弃）、press+drag 合并成 motion，行为碎片化到无法承诺语义；
                # 点击交互用内置拖拽选词（pickup 层），agent 内鼠标交互用 e 全屏
                button = 64 if wheel_up else 65
                embed.send_literal(
                    emb.name,
                    embed.sgr_mouse_sequence(button, mx - pane_x0 + 1, my + 1),
                )
            return False
        if wheel_up or wheel_down:
            if not emb.copy_mode:
                embed.copy_mode_enter(emb.name)
                emb.copy_mode = True
            embed.copy_mode_scroll(emb.name, up=wheel_up)
            emb.poke.set()
        return False

    def _forward_pane_key(ch: int) -> None:
        """pane 聚焦时的按键总入口：除 C-\ 已在主循环拦截外，全部发往托管会话。"""
        if not emb.name or emb.dead:
            return
        # 滚轮翻历史进入的 copy-mode：任何按键先退出再把键发给程序（tmux 原生
        # copy-mode 会吃掉普通键，用户接着打字时字符会丢，体验上像「键盘失灵」）
        if emb.copy_mode and ch != curses.KEY_MOUSE:
            embed.copy_mode_cancel(emb.name)
            emb.copy_mode = False
        if 32 <= ch <= 255 and ch != 127:
            _drain_input(ch)
        elif ch == 27:
            _forward_escape()
        elif ch in (curses.KEY_RESIZE, curses.KEY_MOUSE):
            return  # resize 由绘制路径处理；鼠标统一走主循环的 _pane_mouse
        else:
            translated = embed.translate_key(ch)
            if translated is not None:
                embed.send_key(emb.name, translated[1])
        emb.poke.set()

    def _suspend_self() -> None:
        """列表/侧栏里的 C-z：把 pickup 自己挂起；回前台后恢复 raw 模式与按键解码。"""
        curses.def_prog_mode()
        curses.endwin()
        os.kill(os.getpid(), signal.SIGSTOP)
        curses.reset_prog_mode()
        stdscr.keypad(True)
        stdscr.timeout(200)

    # getch 超时为 200ms，每 5 帧（约 1 秒）轮询一次缓存文件，拾取后台进程产出的新标题。
    POLL_EVERY = 5
    REFRESH_INTERVAL = 3.0  # 秒，后台重扫会话列表的间隔

    def _background_refresh() -> None:
        """周期性重扫磁盘发现新增/结束的会话；只在集合真的变化时唤醒主循环重绘，
        守护线程随进程退出自然结束，无需显式停止。"""
        while True:
            time.sleep(REFRESH_INTERVAL)
            try:
                if store.refresh():
                    store.dirty.set()
            except OSError:
                pass  # 磁盘短暂不可读（如并发写入）时跳过本轮，下次重试

    threading.Thread(target=_background_refresh, daemon=True).start()

    # 记一次初始选中项：用户在首次后台重扫（约 REFRESH_INTERVAL 秒）前未按任何键时，
    # 也要有定位基准，否则 _relocate_after_refresh 无从下手。
    _, _initial_width = stdscr.getmaxyx()
    _remember_selection(_sidebar_width(store.projects(), _initial_width) > 0)

    frame = 0
    had_key = True
    last_emb_gen = -2
    last_size: tuple[int, int] | None = None

    while True:
        if frame % POLL_EVERY == 0:
            store.poll_cache_updates()

        height_now, full_width = stdscr.getmaxyx()
        width = _embed_list_width(full_width, height_now, emb)
        sidebar_visible = _sidebar_width(store.projects(), width) > 0
        if not sidebar_visible and ui.focus == "sidebar":
            # 终端被拖窄导致侧边栏隐藏：焦点强制回列表；proj_idx 保留但过滤旁路，拉宽后自动恢复
            ui.focus = "list"
        if emb.dead and ui.focus == "pane":
            ui.focus = "list"  # 面板里的会话进程退出：焦点弹回列表，占位文案由绘制路径给出
        elif width == full_width and ui.focus == "pane":
            # 终端被拖窄导致面板本身不可见（不同于会话已死）：焦点也必须弹回列表，
            # 否则按键（包括 q）会被静默转发进这个看不见的托管会话，表现像键盘
            # 失灵，只能靠 C-\ 逃生（真实故障，已复现）。拉宽后用户可再次回车聚焦。
            ui.focus = "list"

        dirty = store.dirty.is_set()
        if dirty:
            store.dirty.clear()
            _relocate_after_refresh(sidebar_visible)

        # 没事发生就整帧跳过重绘：无按键、无 dirty、面板 generation 没变、终端尺寸没变、
        # 也没有标题在转圈时，上一帧的内容保持不动。以前每 200ms 全量重绘一次（含
        # 8k 单元格的面板区），是 pane 聚焦时输入延迟的主要来源之一
        _, generating_now = store.snapshot()
        with emb.lock:
            emb_gen = emb.generation if emb.active else -1
        size_now = (height_now, full_width)
        if (
            had_key or dirty or generating_now or frame == 0
            or emb_gen != last_emb_gen or size_now != last_size
        ):
            _draw(stdscr, store, ui, frame, emb, pool)
            last_emb_gen = emb_gen
            last_size = size_now
        had_key = False
        frame += 1

        # pane 聚焦时把 getch 超时压到 50ms：capture 线程抓到回显只能靠超时自然醒来
        # （200ms 意味着回显平均白等 100ms），这是内嵌输入延迟里最大的一项；
        # 50ms 仍足以让 ncurses 拼完被 SSH 拆开的转义序列。列表/侧栏聚焦时维持 200ms。
        stdscr.timeout(50 if ui.focus == "pane" else 200)
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if ch == -1:
            continue  # timeout，有变化时下一帧自然会画
        had_key = True
        if ch != curses.KEY_MOUSE:
            emb.sel_anchor = emb.sel_start = emb.sel_end = None  # 任意键盘输入清除选词高亮
            emb.sel_zone = None

        sessions = _visible_sessions(store, ui, sidebar_visible)
        ui.idx = max(0, min(ui.idx, len(sessions) - 1)) if sessions else 0
        ui.proj_idx = max(0, min(ui.proj_idx, len(store.projects())))

        # 注意：不能把裸 ESC(27) 也绑定为退出键。stdscr.timeout(200) 让 getch
        # 处于非阻塞模式，这种模式下 ncurses 无法安全等待去判断"单独的 ESC"和
        # "方向键转义序列的开头"，会把序列的第一个字节直接当成裸 ESC 返回，
        # 导致方向键失灵。所以退出键只留 q（pane 聚焦时 ESC 会透传给会话）。
        if ui.focus == "pane":
            if ch == 28:  # C-\：焦点回列表，托管会话在后台 tmux 里继续跑
                if emb.copy_mode:
                    embed.copy_mode_cancel(emb.name)
                    emb.copy_mode = False
                ui.focus = "list"
            elif ch == curses.KEY_MOUSE:
                # 鼠标事件不置 had_key：拖拽/移动事件流仅转发或丢弃，若每个事件都
                # 触发整帧重绘，拖拽瞬间变成重绘风暴（实测卡死的根源）
                had_key = _pane_mouse()
            else:
                _forward_pane_key(ch)
        elif ch == ord("q"):
            return None
        elif ch == 3:
            # raw 模式下 C-c 只是字节 3 而不再是 SIGINT；列表/侧栏里维持"退出 pickup"的习惯
            return None
        elif ch == 26:
            _suspend_self()  # C-z 挂起 pickup 自己（pane 聚焦时已在上面的分支透传给会话）
        elif ch == ord("\t"):
            current = runtime_ids.index(ui.source)
            ui.source = runtime_ids[(current + 1) % len(runtime_ids)]
            ui.focus = "list"
            ui.idx = 0
            ui.top = 0
        elif ui.focus == "sidebar":
            if ch in (curses.KEY_UP, ord("k")):
                ui.proj_idx = max(0, ui.proj_idx - 1)
                ui.idx = 0
                ui.top = 0
            elif ch in (curses.KEY_DOWN, ord("j")):
                ui.proj_idx = min(len(store.projects()), ui.proj_idx + 1)
                ui.idx = 0
                ui.top = 0
            elif ch == curses.KEY_RIGHT:
                ui.focus = "list"
            elif ch in (10, 13, curses.KEY_ENTER):
                ui.focus = "list"
            elif ch in (ord("a"), ord("n")):
                # a 在侧边栏没有具体会话，_session_action 会 beep 拒绝；n 靠选中的项目目录新建
                result = _session_action(ch, stdscr, store, ui, None, sidebar_visible)
                if result is not _ACTION_STAY and result is not _ACTION_PASS:
                    if _embed_usable_here():
                        _embed_open(result)
                    else:
                        return result
            elif ch == curses.KEY_MOUSE:
                had_key = _pane_mouse()  # 选词/滚轮统一入口（拖拽选词在全屏任意焦点可用）
            elif ch == ord("m"):
                # 切换全局鼠标上报：关闭后恢复终端原生框选/复制（开启期间原生
                # 框选被程序吃掉是协议固有限制）；pane 聚焦时 m 原样发给会话
                emb.mouse_report = not emb.mouse_report
                _apply_mousemask(emb.mouse_report)
            # KEY_LEFT：已经是侧边栏端点，停住不动（Tab 仍可循环切换来源）
        else:  # ui.focus == "list"
            if ch in (curses.KEY_UP, ord("k")):
                if sessions:
                    ui.idx = max(0, ui.idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                if sessions:
                    ui.idx = min(len(sessions) - 1, ui.idx + 1)
            elif ch == curses.KEY_LEFT:
                current = runtime_ids.index(ui.source)
                if sidebar_visible and current == 0:
                    ui.focus = "sidebar"
                else:
                    ui.source = runtime_ids[(current - 1) % len(runtime_ids)]
                    ui.idx = 0
                    ui.top = 0
            elif ch == curses.KEY_RIGHT:
                current = runtime_ids.index(ui.source)
                if not (sidebar_visible and current == len(runtime_ids) - 1):
                    ui.source = runtime_ids[(current + 1) % len(runtime_ids)]
                    ui.idx = 0
                    ui.top = 0
                # 侧边栏可见且已在最后一个来源：端点停住不回绕（Tab 仍可循环切换）
            elif ch == ord(" "):
                if sessions:
                    session = sessions[ui.idx]
                    title = store.get_title(session)
                    preview_result = _show_preview(stdscr, store, ui, session, title, sidebar_visible,
                                                   mouse_report=emb.mouse_report)
                    if preview_result is not None:
                        request, force_fullscreen = preview_result
                        if not force_fullscreen and _embed_usable_here():
                            _embed_open(request)
                        else:
                            return request
            elif ch in (10, 13, curses.KEY_ENTER):
                if sessions:
                    session = sessions[ui.idx]
                    request = LaunchRequest(session, ui.source, store.get_title(session))
                    if _embed_usable_here():
                        _embed_open(request)
                    else:
                        return request
            elif ch == ord("e"):
                # 全屏接管：保留给需要鼠标交互/大屏细看/整段复制的场景（execvp 路径不变）
                if sessions:
                    session = sessions[ui.idx]
                    return LaunchRequest(session, ui.source, store.get_title(session))
            elif ch == ord("c") and emb.active:
                # 关闭分栏回到全宽列表；托管会话在后台 tmux 里继续跑，回车可再接回
                emb.active = False
                emb.name = None
                emb.grid = None
                emb.cursor = None
                emb.copy_mode = False
                emb.session_key = None
                embed.close_channel()
            elif ch == curses.KEY_MOUSE:
                had_key = _pane_mouse()  # 选词/滚轮统一入口（拖拽选词在全屏任意焦点可用）
            elif ch == ord("m"):
                emb.mouse_report = not emb.mouse_report
                _apply_mousemask(emb.mouse_report)
            else:
                session = sessions[ui.idx] if sessions else None
                result = _session_action(ch, stdscr, store, ui, session, sidebar_visible)
                if result is not _ACTION_STAY and result is not _ACTION_PASS:
                    if _embed_usable_here():
                        _embed_open(result)
                    else:
                        return result

        # 光标移动、切换来源或切换项目都可能改变可见区，统一在这里对齐渲染滚动位置
        _sync_top()
        _sync_sidebar_top()
        # 记录本次按键后用户实际停留的会话/项目，供下次后台重扫触发的 dirty 事件定位光标
        _remember_selection(sidebar_visible)


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

    供大模型或自动化脚本调用：不启动 curses TUI，不触发后台标题生成，
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


def _dispatch_direct_launch(argv: list[str], registry: RuntimeRegistry) -> None:
    """处理 `pickup [--no-keepalive] <runtime> [参数…]` 直启透传子命令。

    参数原样交给底层运行时（`registry.build_passthrough_plan` 只垫上默认全自动放行参数，
    用户已显式带了就不重复），默认包进后台保活，`--no-keepalive` 可临时关闭。
    """
    _require_tmux()
    no_keepalive = argv and argv[0] == "--no-keepalive"
    rest = argv[1:] if no_keepalive else argv
    runtime_id, user_args = rest[0], rest[1:]
    plan = registry.build_passthrough_plan(runtime_id, user_args)
    if keepalive.enabled(no_keepalive):
        plan = keepalive.wrap_plan(plan, runtime_id, keepalive.new_session_ident())
    try:
        execute_launch(plan)
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # list/search/show/context/plan/describe 是面向 Agent 的机器可读子命令，整体转发给
    # agent_api，不与下面的 TUI/--json 旧参数共用同一个 parser。
    if len(sys.argv) > 1 and sys.argv[1] in agent_api.COMMAND_ROOT_NAMES:
        sys.exit(agent_api.dispatch(sys.argv[1:]))

    # `pickup claude …` / `pickup codex …`（可选前置 --no-keepalive）是直启透传子命令，同样整体
    # 绕开下面的 TUI/--json 旧参数 parser，此处只需运行时 ID 集合，不做真实扫描。
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
            "默认启动交互式 TUI（curses），需要真实终端；非真实终端自动退化为 JSON。\n"
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

    # 没有真实终端（管道、脚本、被 Agent 直接调用）时，curses 无法初始化；自动退化
    # 为 JSON 列表而不是崩溃。stdin/stdout 分开检测：任一端不是真实终端都不能进 TUI。
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

    # 趁 curses 接管前探测外层终端的前景/背景色（OSC 10/11）：内嵌面板聚焦时经
    # refresh-client -r 注入托管 pane，让 pane 内 agent 的深/浅主题检测拿到真实值
    global _OSC_REPORT
    _OSC_REPORT = _probe_osc_colours()
    if os.environ.get("PICKUP_DEBUG"):
        print(f"[pickup debug] 外层终端 OSC 10/11 探测: {_OSC_REPORT!r} "
              f"(tmux={'是' if os.environ.get('TMUX') else '否'}, "
              f"refresh -r 支持={'是' if embed.supports_theme_report() else '否'})",
              file=sys.stderr)

    chosen = curses.wrapper(_run, store, embed.available(args.no_keepalive))
    # 兜底关闭内嵌控制通道：pane 聚焦时打开的 `tmux -C attach` 控制 client 只有
    # c 键关分栏才会关，q 退出/回车全屏接管等退出路径不经那条分支——不在这里统一
    # 兜底就会把孤儿控制 client 留在保活服务端上。close_channel 无通道时是空操作。
    embed.close_channel()
    # 关闭 bracketed paste 模式，把终端原样还给后续的 execvp 或 shell
    sys.stdout.write("\x1b[?2004l")
    sys.stdout.flush()
    if chosen is None:
        return

    try:
        _launch(chosen, store.registry, keepalive_on)
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
