"""TUI 多语言：默认英文；系统语言为中文时自动用中文。

机器接口（`pickup list` 等 JSON / 退出码）保持英文契约，不走本模块。
覆盖语言：环境变量 `PICKUP_LANG=en|zh`（或 `zh_CN` / `zh-Hans` 等）。
"""

from __future__ import annotations

import os
import re
from typing import Mapping

SUPPORTED = ("en", "zh")
DEFAULT_LANG = "en"

# 文案表：key → 各语言译文。英文是默认兜底。
_MESSAGES: dict[str, dict[str, str]] = {
    "action.advanced": {
        "en": "Advanced",
        "zh": "高级操作",
    },
    "action.new": {
        "en": "New",
        "zh": "新建",
    },
    "action.fullscreen": {
        "en": "Fullscreen",
        "zh": "全屏",
    },
    "action.kill_session": {
        "en": "End session",
        "zh": "结束会话",
    },
    "action.close_pane": {
        "en": "Close pane",
        "zh": "关闭面板",
    },
    "action.mouse": {
        "en": "Mouse",
        "zh": "鼠标",
    },
    "action.screenshot": {
        "en": "Screenshot",
        "zh": "截图",
    },
    "action.quit": {
        "en": "Quit",
        "zh": "退出",
    },
    "action.preview_home": {
        "en": "Preview top",
        "zh": "预览顶",
    },
    "action.preview_end": {
        "en": "Preview bottom",
        "zh": "预览底",
    },
    "action.preview_page_up": {
        "en": "Preview page up",
        "zh": "预览上翻",
    },
    "action.preview_page_down": {
        "en": "Preview page down",
        "zh": "预览下翻",
    },
    "action.select": {
        "en": "Select",
        "zh": "选择",
    },
    "action.preview_home": {
        "en": "Preview top",
        "zh": "预览顶部",
    },
    "action.preview_end": {
        "en": "Preview bottom",
        "zh": "预览底部",
    },
    "action.preview_page_up": {
        "en": "Preview page up",
        "zh": "预览上翻",
    },
    "action.preview_page_down": {
        "en": "Preview page down",
        "zh": "预览下翻",
    },
    "filter.placeholder": {
        "en": "Filter projects…",
        "zh": "筛选项目…",
    },
    "filter.placeholder_count": {
        "en": "Filter projects ({count})",
        "zh": "筛选项目 ({count})",
    },
    "filter.placeholder_count_active": {
        "en": "Filter projects… ({count})",
        "zh": "筛选项目… ({count})",
    },
    "filter.load_error": {
        "en": "Filter projects… — {error}; retrying",
        "zh": "筛选项目… — {error}；正在自动重试",
    },
    "filter.no_sessions": {
        "en": "Filter projects… — no {names} sessions found",
        "zh": "筛选项目… — 未找到任何 {names} 会话记录",
    },
    "list.new_session": {
        "en": "+ New session",
        "zh": "＋ 新建会话",
    },
    "status.running": {
        "en": "Running",
        "zh": "运行中",
    },
    "status.running_hosted": {
        "en": "Running (hosted)",
        "zh": "运行中(托管)",
    },
    "status.ended": {
        "en": "Ended",
        "zh": "已结束",
    },
    "project.unknown": {
        "en": "Unknown project",
        "zh": "未知项目",
    },
    "project.unknown_dir": {
        "en": "(unknown directory)",
        "zh": "(未知目录)",
    },
    "project.current_dir": {
        "en": "Current directory",
        "zh": "当前目录",
    },
    "detail.new_session_hint": {
        "en": "New session: pick a project and runtime",
        "zh": "新建会话：选择项目与运行时",
    },
    "detail.pick_session": {
        "en": "Select a session to view details",
        "zh": "选择一个会话查看详情",
    },
    "detail.session_ended": {
        "en": "Session ended (Enter to open another)",
        "zh": "会话已结束（回车打开其他会话）",
    },
    "detail.empty_preview": {
        "en": "No user messages or final replies to preview",
        "zh": "没有可预览的用户消息或最终答复",
    },
    "preview.you": {
        "en": "● You",
        "zh": "● 你",
    },
    "modal.menu_hint": {
        "en": "↑↓ Select   Enter Confirm   Esc Back",
        "zh": "↑↓ 选择   Enter 确认   Esc 返回",
    },
    "modal.confirm_hint": {
        "en": "q Confirm   any other key Cancel",
        "zh": "q 确认   其他键取消",
    },
    "modal.not_installed": {
        "en": "{action} (not installed)",
        "zh": "{action}［未安装］",
    },
    "modal.native_resume": {
        "en": "Native resume (full context)",
        "zh": "原生恢复（保留完整上下文）",
    },
    "modal.read_history_new": {
        "en": "Read {source} history, then start a new session",
        "zh": "读取 {source} 历史后新建会话",
    },
    "modal.blank_in_dir": {
        "en": "Start a blank session in this directory",
        "zh": "在该目录下新建空白会话",
    },
    "modal.handoff_title": {
        "en": "Advanced: choose handoff runtime",
        "zh": "高级操作：选择接力运行时",
    },
    "modal.new_runtime_title": {
        "en": "New session: choose runtime",
        "zh": "新建会话：选择运行时",
    },
    "modal.new_project_title": {
        "en": "New session: choose project",
        "zh": "新建会话：选择项目",
    },
    "confirm.kill_session": {
        "en": "End session “{title}”? Unsaved progress in the current task will be lost",
        "zh": "结束会话「{title}」？未保存的当前任务进度将丢失",
    },
    "confirm.hint_q": {
        "en": "q confirm   any other key cancel",
        "zh": "q 确认   其他键取消",
    },
    "notify.screenshot": {
        "en": "Screenshot saved: {path}",
        "zh": "已截图 {path}",
    },
    "time.just_now": {
        "en": "just now",
        "zh": "刚刚",
    },
    "time.minutes_ago": {
        "en": "{n}m ago",
        "zh": "{n}分钟前",
    },
    "time.hours_ago": {
        "en": "{n}h ago",
        "zh": "{n}小时前",
    },
    "store.load_failed": {
        "en": "Failed to load sessions: {error}",
        "zh": "会话加载失败：{error}",
    },
    "store.refresh_failed": {
        "en": "Failed to refresh sessions: {error}",
        "zh": "会话刷新失败：{error}",
    },
    "list_separator": {
        "en": ", ",
        "zh": "、",
    },
}

_lang: str = DEFAULT_LANG
_initialized = False


def _normalize_lang(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    if not text or text in ("C", "POSIX"):
        return None
    # en_US.UTF-8 / zh-Hans / zh_CN → 主语言码
    primary = re.split(r"[_.@]", text.replace("-", "_"), maxsplit=1)[0].lower()
    if primary == "zh":
        return "zh"
    if primary == "en":
        return "en"
    return None


def detect_lang(env: Mapping[str, str] | None = None) -> str:
    """从环境推断界面语言：默认 en；zh* → zh。

    优先级：PICKUP_LANG → LC_ALL → LC_MESSAGES → LANG → LANGUAGE。
    """
    environ = env if env is not None else os.environ
    override = _normalize_lang(environ.get("PICKUP_LANG"))
    if override in SUPPORTED:
        return override
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        found = _normalize_lang(environ.get(key))
        if found in SUPPORTED:
            return found
    # LANGUAGE 可能是 "zh_CN:en_US:en" 这类冒号列表
    language = environ.get("LANGUAGE") or ""
    for part in language.split(":"):
        found = _normalize_lang(part)
        if found in SUPPORTED:
            return found
    return DEFAULT_LANG


def init(lang: str | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """初始化当前语言；可显式传入，否则按环境检测。可重复调用。"""
    global _lang, _initialized
    if lang is not None:
        normalized = _normalize_lang(lang) or DEFAULT_LANG
        _lang = normalized if normalized in SUPPORTED else DEFAULT_LANG
    else:
        _lang = detect_lang(env)
    _initialized = True
    return _lang


def set_lang(lang: str) -> str:
    """测试或运行时切换语言。"""
    return init(lang)


def get_lang() -> str:
    if not _initialized:
        init()
    return _lang


def t(key: str, **kwargs: object) -> str:
    """按当前语言取文案；缺 key 时回退英文，再回退 key 本身。"""
    if not _initialized:
        init()
    catalog = _MESSAGES.get(key, {})
    template = catalog.get(_lang) or catalog.get(DEFAULT_LANG) or key
    if kwargs:
        return template.format(**kwargs)
    return template


def join_names(names: list[str]) -> str:
    """按当前语言连接运行时/项目名列表。"""
    return t("list_separator").join(names)


# 导入即按环境初始化，便于类体里的 Binding 描述在首次加载时已是对的语言。
init()
