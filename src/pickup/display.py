"""终端展示工具：相对时间、宽字符对齐、预览排版、会话筛选与项目标签。"""

from __future__ import annotations

import os
import unicodedata
from datetime import datetime

from rich.cells import cell_len as _rich_cell_len, chop_cells as _rich_chop_cells

from pickup.models import ConversationMessage, format_message_time, session_key

def _format_relative_time(mtime: float, now: float | None = None) -> str:
    """把时间戳渲染成人性化相对时间；超过一天退回绝对日期时间。

    展示层专用，只在 TUI 渲染时现算，不写回 display_time（后者保持绝对格式，
    供 --json 与单测稳定消费）。
    """
    if now is None:
        now = datetime.now().timestamp()
    delta = now - mtime
    from pickup.i18n import t

    if delta < 60:  # 含未来时间 / 时钟漂移导致的负值
        return t("time.just_now")
    if delta < 3600:
        return t("time.minutes_ago", n=int(delta // 60))
    if delta < 86400:
        return t("time.hours_ago", n=int(delta // 3600))
    return datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")



def _char_width(ch: str) -> int:
    # 与 `embed._char_width`、Rich/Textual 的渲染宽度表保持一致（`rich.cells.cell_len`）：
    # 自实现的 `unicodedata.east_asian_width` 在 emoji、组合字符、ambiguous-width
    # 字符上会跟 Rich 的排版结果不一致，本项目同时用这两套计算（列表卡片排版
    # 和内嵌画面渲染各自有一份），会导致 CJK/emoji 对齐错位。
    return _rich_cell_len(ch)


def _text_width(text: str) -> int:
    # cell_len 直接对整段文本计算（内部已经处理了宽字符/组合字符的展开），
    # 比逐字符调用 cell_len 再求和更准也更省——逐字符调用在 emoji 等需要
    # 上下文判断的场景下反而会算错。
    return _rich_cell_len(text)


def _fit_cell(text: object, width: int, *, ellipsis: bool = False) -> str:
    """按终端显示宽度截断并补齐，避免中文和图标把表格列挤歪。

    ellipsis=True 时，放不下的尾部换成 `...`（按显示宽度计算，CJK/emoji 安全）。
    """
    if width <= 0:
        return ""
    raw = str(text)
    if ellipsis and _text_width(raw) > width:
        marker = "..."
        if width <= _text_width(marker):
            chunks = _rich_chop_cells(marker, width)
            fitted = chunks[0] if chunks else ""
        else:
            body = (_rich_chop_cells(raw, width - _text_width(marker)) or [""])[0]
            fitted = body + marker
        return fitted + " " * (width - _text_width(fitted))
    chunks = _rich_chop_cells(raw, width)
    fitted = chunks[0] if chunks else ""
    return fitted + " " * (width - _text_width(fitted))


def _fit_cell_right(text: object, width: int) -> str:
    """按终端显示宽度截断并右对齐补齐（数值列用）。"""
    if width <= 0:
        return ""
    chunks = _rich_chop_cells(str(text), width)
    fitted = chunks[0] if chunks else ""
    return " " * (width - _text_width(fitted)) + fitted


def _wrap_preview_text(text: str, width: int) -> list[str]:
    """按终端显示宽度折行，并移除会破坏 TUI 的控制字符。"""
    if width <= 0:
        return []

    # ZWNJ/ZWJ 虽属 Cf，但是文字连写和 emoji grapheme 的有效组成
    # 字符，不能像其他控制字符一样替换为空格。
    cleaned = "".join(
        ch if ch in "\n\t\u200c\u200d" or unicodedata.category(ch)[0] != "C" else " "
        for ch in text
    ).replace("\t", "    ")
    lines: list[str] = []
    for paragraph in cleaned.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_rich_chop_cells(paragraph, width))
    return lines


def _preview_lines(
    messages: list[ConversationMessage], runtime_name: str, width: int,
) -> list[tuple[str, str, str]]:
    """把真实会话消息整理为带角色样式的聊天记录行。

    每行是 (kind, text, dim_suffix) 三元组：首行格式为「角色: 消息内容」，续行按角色
    前缀宽度缩进对齐正文；kind 为 user/assistant，渲染时整段（含正文）同色。
    dim_suffix 只挂在首行（发送时间，淡色叠绘）；消息缺时间戳或续行留空。
    """
    content_width = max(1, width - 2)
    from pickup.i18n import t

    if not messages:
        return [("dim", t("detail.empty_preview"), "")]

    lines: list[tuple[str, str, str]] = []
    for message in messages:
        if lines:
            lines.append(("blank", "", ""))
        time_suffix = f"  · {format_message_time(message.timestamp)}" if message.timestamp else ""
        if message.role == "user":
            kind = "user"
            role = t("preview.you")
        else:
            kind = "assistant"
            role = f"◆ {runtime_name}"
        prefix = f"{role}: "
        body_width = max(1, content_width - _text_width(prefix))
        wrapped = _wrap_preview_text(message.text.strip(), body_width) or [""]
        indent = " " * _text_width(prefix)
        for i, part in enumerate(wrapped):
            if i == 0:
                lines.append((kind, f"{prefix}{part}", time_suffix))
            else:
                lines.append((kind, f"{indent}{part}", ""))
    return lines


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille 转圈圈，每帧占 1 列宽

class _LocalizedLabel:
    """可当字符串用的惰性文案：比较/拼接时按当前语言求值。"""

    def __init__(self, key: str) -> None:
        self._key = key

    def __str__(self) -> str:
        from pickup.i18n import t

        return t(self._key)

    def __eq__(self, other: object) -> bool:
        return str(self) == other

    def __hash__(self) -> int:
        return hash(self._key)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __add__(self, other: object) -> str:
        return str(self) + str(other)

    def __radd__(self, other: object) -> str:
        return str(other) + str(self)


UNKNOWN_PROJECT_LABEL = _LocalizedLabel("project.unknown_dir")


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


def _fuzzy_match(query: str, *texts: str) -> bool:
    """大小写无关模糊匹配：子串包含，或查询字符按序出现（子序列）。

    空查询视为匹配全部。用于侧边栏项目搜索框过滤会话。
    """
    needle = (query or "").casefold().strip()
    if not needle:
        return True
    for raw in texts:
        hay = (raw or "").casefold()
        if not hay:
            continue
        if needle in hay:
            return True
        it = iter(hay)
        if all(ch in it for ch in needle):
            return True
    return False


def _session_project_label(session: dict) -> str:
    """会话所属项目的展示名（cwd 末级目录；未知目录用统一文案）。"""
    cwd_key = _normalize_cwd(session.get("cwd"))
    if not cwd_key:
        return str(session.get("cwd_display") or UNKNOWN_PROJECT_LABEL)
    base = os.path.basename(cwd_key)
    return base or str(session.get("cwd_display") or UNKNOWN_PROJECT_LABEL)


def _filter_sessions(sessions: list[dict], cwd_key: str | None) -> list[dict]:
    """按归一化工作目录精确匹配过滤；cwd_key 为 None 时原样返回（不过滤）。"""
    if cwd_key is None:
        return sessions
    return [s for s in sessions if _normalize_cwd(s.get("cwd")) == cwd_key]


def _filter_sessions_by_query(
    sessions: list[dict],
    query: str,
    *,
    titles: dict[str, str] | None = None,
) -> list[dict]:
    """按项目名/路径/会话标题做大小写无关模糊过滤；空查询不过滤。"""
    needle = (query or "").strip()
    if not needle:
        return sessions
    titles = titles or {}
    out: list[dict] = []
    for session in sessions:
        cwd_key = _normalize_cwd(session.get("cwd"))
        title = titles.get(session_key(session), "")
        fallback = str(session.get("fallback_title") or "")
        if _fuzzy_match(
            needle,
            _session_project_label(session),
            cwd_key,
            str(session.get("cwd_display") or ""),
            title,
            fallback,
        ):
            out.append(session)
    return out
