#!/usr/bin/env python3
"""sc：终端会话接力工具。

单列表格列出已注册运行时（Claude Code / Codex）的最近会话，左右切换来源、
上下选行，回车后原生恢复，或通过高级操作交给其他运行时接力。

注意：默认启动交互式终端 TUI（curses），需要真实终端，不能被自动化脚本或
大模型直接调用。非真实终端环境（管道、脚本、Agent 调用）会自动退化为 JSON
会话列表。大模型 Agent 需要结构化查询（列表/搜索/详情/接续上下文）时，使用
`sc list` / `sc search` / `sc show` / `sc context` / `sc describe` 子命令，
详见 agent_api.py 和 docs/SKILL.md。

用法：
    sc                  # 启动 TUI（交互式，需要真实终端）
    sc --limit 30        # 每个来源最多列出 30 条
    sc --json            # 输出 JSON 会话列表后退出，不启动 TUI（旧格式，仍保留）
    sc --json --limit 5  # JSON 模式，每个来源最多 5 条
    sc list              # 结构化会话列表（推荐给 Agent 使用，字段更完整）
    sc search 天气 app    # 按关键词搜会话
    sc show <会话ID前缀>  # 查看会话详情和对话内容
    sc context <会话ID前缀>  # 生成接续该会话所需的上下文数据包
    sc describe          # 查看全部子命令的参数与输出字段说明
"""

from __future__ import annotations

import argparse
import curses
import fcntl
import json
import os
import subprocess
import sys
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_api
import titles
from models import ConversationMessage, LaunchRequest, NewSessionRequest, session_key
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
) -> list[tuple[str, str]]:
    """把真实会话消息整理为带角色样式的聊天记录行。"""
    content_width = max(1, width - 2)
    if not messages:
        return [("dim", "没有可预览的用户消息或最终答复")]

    lines: list[tuple[str, str]] = []
    for message in messages:
        if lines:
            lines.append(("blank", ""))
        if message.role == "user":
            lines.append(("user", "● 你"))
        else:
            lines.append(("assistant", f"◆ {runtime_name}"))
        lines.extend(
            ("body", f"  {line}")
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


def _init_colors() -> None:
    """绑定颜色对；终端不支持彩色时静默跳过，退化为单色显示。"""
    global DIM_EXTRA_ATTR
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    # 弱化文字（分隔线/次要列/帮助文字）不能用 curses.COLOR_WHITE 强制写死前景色：
    # 之前固定用白色 + A_DIM，在浅色/白色背景终端里等于「白底写白字再调暗」，
    # 几乎不可读（用户实测反馈）。终端支持 256 色时改用真正的中灰（xterm 256 色
    # 第 244 号，在浅色和深色背景下对比度都够），不再叠加 A_DIM（灰色本身已经
    # 够暗，再叠加会变得过淡）；只有退化到 8/16 色终端时才回退到「终端默认前景
    # 色 + A_DIM」这个次优方案（无法访问 256 色时能做到的最好效果）。
    if curses.COLORS >= 256:
        dim_fg = 244
        DIM_EXTRA_ATTR = 0
    else:
        # 8/16 色终端拿不到真正的中灰：bg=-1（默认色可用）时用终端默认前景色，
        # 至少能随浅色/深色主题自适应；用不了默认色的老终端只能假定黑底、退回
        # 白字，和改动前行为一致。
        dim_fg = -1 if bg == -1 else curses.COLOR_WHITE
        DIM_EXTRA_ATTR = curses.A_DIM

    curses.init_pair(PAIR_TAB_ACTIVE, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_TAB_INACTIVE, dim_fg, bg)
    curses.init_pair(PAIR_DIM, dim_fg, bg)
    curses.init_pair(PAIR_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(PAIR_DONE, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_PENDING, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_ABORTED, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_KEY, curses.COLOR_CYAN, bg)


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

    标题生成已移交独立后台进程（sc --generate-titles），本类只负责读取缓存，
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
        self.conversations: dict[str, list[ConversationMessage]] = {}
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
        # 每个适配器负责按时间倒序返回，无需在界面层二次排序

        with self.lock:
            self.sessions.update(scanned)
            for bucket in scanned.values():
                for session in bucket:
                    key = session_key(session)
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
        """按需读取并缓存选中会话的真实聊天记录。"""
        key = session_key(session)
        with self.lock:
            if key in self.conversations:
                return list(self.conversations[key])
        runtime = self.registry.get(str(session.get("source") or ""))
        messages = runtime.load_conversation(session)
        with self.lock:
            self.conversations[key] = list(messages)
        return messages


@dataclass
class UIState:
    """TUI 主循环状态：当前来源、会话列表光标，以及侧边栏焦点与选中项。"""

    source: str
    idx: int = 0
    top: int = 0
    focus: str = "list"  # "sidebar" | "list"
    proj_idx: int = 0    # 0 = 全部项目（不过滤）
    sb_top: int = 0


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


def _draw(stdscr, store: SessionStore, ui: UIState, frame: int = 0) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    # 终端太小时跳过绘制，避免 addnstr 写到边界外崩溃
    if height < 7 or width < 20:
        return

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

    # 列宽分配：# / 标题 / 目录 / 时间 / 大小 / 状态
    col_num, col_title, col_dir, col_time, col_size, col_status = _column_widths(main_w)

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

    list_height = height - 6  # 顶部4行 + 底部分隔1行 + 帮助1行
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
            prefix = "▸" if current else " "
            num = _fit_cell(f"{prefix}{i + 1}", col_num)
            dir_col = _fit_cell(s["cwd_display"], col_dir)
            time_col = _fit_cell(_format_relative_time(s["mtime"]), col_time)
            size_col = _fit_cell_right(_format_size(s["size_kb"]), col_size)
            status_col = _fit_cell("进行中" if s["live"] else "已结束", col_status)

            if selected:
                base_attr = curses.color_pair(PAIR_SELECTED) | curses.A_BOLD
            elif current:
                base_attr = curses.color_pair(PAIR_TAB_ACTIVE) | curses.A_BOLD
            else:
                base_attr = curses.A_NORMAL
            status_attr = base_attr if current else _status_attr(s["live"])

            y = 4 + row
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

            for cell, attr in (
                *title_segments,
                (COL_GAP, base_attr),
                (dir_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (time_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (size_col, base_attr if current else dim),
                (COL_GAP, base_attr),
                (status_col, status_attr),
            ):
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
        if sidebar_focused:
            help_entries = (
                ("↑↓", " 选项目   "),
                ("→", " 回列表   "),
                ("n", " 新建   "),
                ("Tab", " 切来源   "),
                ("q", " 退出"),
            )
        elif sb_w:
            help_entries = (
                ("↑↓", " 选择   "),
                ("←→", " 切换来源/侧边栏   "),
                ("Tab", " 切来源   "),
                ("Space", " 预览   "),
                ("Enter", " 原生恢复   "),
                ("a", " 高级操作   "),
                ("n", " 新建   "),
                ("q", " 退出"),
            )
        else:
            help_entries = (
                ("↑↓", " 选择   "),
                ("←→/Tab", " 切换来源   "),
                ("Space", " 预览   "),
                ("Enter", " 原生恢复   "),
                ("a", " 高级操作   "),
                ("n", " 新建   "),
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
    stdscr.refresh()


def _draw_preview(
    stdscr,
    messages: list[ConversationMessage],
    title: str,
    runtime_name: str,
    scroll: int,
) -> int:
    """全屏绘制聊天记录，返回修正后的滚动位置。"""
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 5 or width < 20:
        stdscr.refresh()
        return 0

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
    stdscr.addnstr(1, 0, "─" * (width - 1), width - 1, dim)
    for row, (kind, line) in enumerate(lines[scroll:scroll + visible_height]):
        if kind == "user":
            attr = user_attr
        elif kind == "assistant":
            attr = assistant_attr
        elif kind == "dim":
            attr = dim
        else:
            attr = curses.A_NORMAL
        stdscr.addnstr(2 + row, 1, line, inner_width, attr)

    footer_y = height - 2
    stdscr.addnstr(footer_y, 0, "─" * (width - 1), width - 1, dim)
    hint = "↑↓/j/k 滚动  PgUp/PgDn 翻页  Home/End 首尾  Enter 恢复  a 接力  n 新建  Space/q 关闭"
    stdscr.addnstr(footer_y + 1, 0, hint, width - 1, key_attr)
    if max_scroll:
        progress = f"{scroll + 1}/{max_scroll + 1}"
        progress_x = max(0, width - 1 - _text_width(progress))
        stdscr.addnstr(footer_y + 1, progress_x, progress, len(progress), dim)
    stdscr.refresh()
    return scroll


def _show_preview(
    stdscr,
    store: SessionStore,
    ui: UIState,
    session: dict,
    title: str,
    sidebar_visible: bool,
) -> LaunchRequest | NewSessionRequest | None:
    """打开全屏聊天记录；回车原生恢复，空格或 q 关闭，`a`/`n` 等会话级快捷键与列表页一致。"""
    messages = store.get_conversation(session)
    runtime_name = store.registry.get(str(session.get("source") or "")).display_name
    scroll = 10 ** 9  # 聊天预览默认定位到最近一轮
    while True:
        scroll = _draw_preview(stdscr, messages, title, runtime_name, scroll)
        try:
            ch = stdscr.getch()
        except curses.error:
            continue
        if ch == -1:
            continue
        if ch in (ord(" "), ord("q")):
            stdscr.clear()
            return None
        if ch in (10, 13, curses.KEY_ENTER):
            stdscr.clear()
            return LaunchRequest(session, ui.source, title)
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
        else:
            result = _session_action(ch, stdscr, store, ui, session, sidebar_visible)
            if result is not _ACTION_STAY and result is not _ACTION_PASS:
                stdscr.clear()
                return result


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


def _session_action(
    ch: int,
    stdscr,
    store: SessionStore,
    ui: UIState,
    session: dict | None,
    sidebar_visible: bool,
):
    """处理列表页与预览页共用的会话级快捷键：`a` 接力、`n` 新建会话。

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


def _run(stdscr, store: SessionStore) -> LaunchRequest | NewSessionRequest | None:
    curses.curs_set(0)
    _init_colors()
    stdscr.keypad(True)  # 关键：没有这行，方向键的 ESC 序列不会被解码成 KEY_LEFT/RIGHT/UP/DOWN，
    # 裸 ESC(27) 会被退出键判断提前吃掉，导致方向键失灵
    stdscr.timeout(200)  # 非阻塞 getch，留出空间检测后台标题刷新

    runtime_ids = store.registry.ids
    source = next((runtime_id for runtime_id in runtime_ids if store.sessions[runtime_id]), runtime_ids[0])
    ui = UIState(source=source)

    def _sync_top() -> None:
        """更新 ui.top 与可见区对齐，保持会话列表状态和实际渲染同步。"""
        height, _ = stdscr.getmaxyx()
        list_height = max(1, height - 6)
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

    frame = 0
    # getch 超时为 200ms，每 5 帧（约 1 秒）轮询一次缓存文件，拾取后台进程产出的新标题。
    POLL_EVERY = 5

    while True:
        if frame % POLL_EVERY == 0:
            store.poll_cache_updates()
        if store.dirty.is_set():
            store.dirty.clear()

        _, width = stdscr.getmaxyx()
        sidebar_visible = _sidebar_width(store.projects(), width) > 0
        if not sidebar_visible and ui.focus == "sidebar":
            # 终端被拖窄导致侧边栏隐藏：焦点强制回列表；proj_idx 保留但过滤旁路，拉宽后自动恢复
            ui.focus = "list"

        _draw(stdscr, store, ui, frame)
        frame += 1

        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if ch == -1:
            continue  # timeout，回去重绘以便标题刷新生效

        sessions = _visible_sessions(store, ui, sidebar_visible)
        ui.idx = max(0, min(ui.idx, len(sessions) - 1)) if sessions else 0
        ui.proj_idx = max(0, min(ui.proj_idx, len(store.projects())))

        # 注意：不能把裸 ESC(27) 也绑定为退出键。stdscr.timeout(200) 让 getch
        # 处于非阻塞模式，这种模式下 ncurses 无法安全等待去判断"单独的 ESC"和
        # "方向键转义序列的开头"，会把序列的第一个字节直接当成裸 ESC 返回，
        # 导致方向键失灵。所以退出键只留 q。
        if ch == ord("q"):
            return None
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
                    return result
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
                    result = _show_preview(stdscr, store, ui, session, title, sidebar_visible)
                    if result is not None:
                        return result
            elif ch in (10, 13, curses.KEY_ENTER):
                if sessions:
                    session = sessions[ui.idx]
                    return LaunchRequest(session, ui.source, store.get_title(session))
            else:
                session = sessions[ui.idx] if sessions else None
                result = _session_action(ch, stdscr, store, ui, session, sidebar_visible)
                if result is not _ACTION_STAY and result is not _ACTION_PASS:
                    return result

        # 光标移动、切换来源或切换项目都可能改变可见区，统一在这里对齐渲染滚动位置
        _sync_top()
        _sync_sidebar_top()


def _launch(request: LaunchRequest | NewSessionRequest, registry: RuntimeRegistry) -> None:
    """生成启动计划并让目标运行时接管当前终端。"""
    if isinstance(request, NewSessionRequest):
        plan = registry.build_new_session_plan(request)
    else:
        plan = registry.build_launch_plan(request)
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
    """脱离 TUI 的独立标题生成进程入口（sc --generate-titles）。

    用文件锁保证全机单实例：拿不到锁说明已有后台进程在跑，直接退出，
    避免用户反复进 sc 堆积多个生成进程、重复消耗 Claude 额度。
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


def main() -> None:
    # list/search/show/context/describe 是面向 Agent 的机器可读子命令，整体转发给
    # agent_api，不与下面的 TUI/--json 旧参数共用同一个 parser。
    if len(sys.argv) > 1 and sys.argv[1] in agent_api.COMMAND_NAMES:
        sys.exit(agent_api.dispatch(sys.argv[1:]))

    parser = argparse.ArgumentParser(
        description=(
            "sc：终端会话接力工具。\n"
            "列出 Claude Code / Codex 最近的会话，选择后原生恢复或跨运行时接力。\n"
            "默认启动交互式 TUI（curses），需要真实终端；非真实终端自动退化为 JSON。\n"
            "大模型 Agent 结构化查询请用 list/search/show/context/describe 子命令。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  sc                 # 启动 TUI，交互式选择并接管终端\n"
            "  sc --json          # 输出 JSON 会话列表后退出，不启动 TUI（旧格式）\n"
            "  sc --json --limit 5  # JSON 模式，每个运行时最多 5 条\n"
            "  sc describe        # 查看 list/search/show/context 等子命令的用法\n"
            "\n"
            "JSON 输出字段说明：\n"
            "  runtime        运行时标识（claude / codex）\n"
            "  id             会话 ID\n"
            "  title          会话标题（本地临时兜底，不调用 AI）\n"
            "  cwd            原会话工作目录\n"
            "  time           最后更新时间（人类可读）\n"
            "  mtime          最后更新时间（Unix 时间戳）\n"
            "  size_kb        历史文件大小（KB）\n"
            "  status         会话状态（已完成 / 待回复 / 已中断）\n"
            "  resume_command 恢复该会话的完整 shell 命令（可直接执行）\n"
            "  history_path   历史 JSONL 文件路径\n"
        ),
    )
    parser.add_argument("--limit", type=int, default=50, help="每个来源最多列出多少条")
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="以 JSON 格式输出会话列表后退出，不启动 TUI")
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

    store = SessionStore(limit=args.limit, registry=registry)
    store.load()

    if not any(store.sessions.values()):
        names = "、".join(runtime.display_name for runtime in store.registry)
        print(f"未找到任何 {names} 会话记录。", file=sys.stderr)
        sys.exit(1)

    # 拉起脱离终端的后台进程生成标题：用户秒退或原生恢复（execvp 替换进程）后仍继续，
    # TUI 通过轮询缓存文件拾取它逐批写入的标题。
    _spawn_title_daemon(args.limit)

    chosen = curses.wrapper(_run, store)
    if chosen is None:
        return

    try:
        _launch(chosen, store.registry)
    except LaunchError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
