#!/usr/bin/env python3
"""扫描 Claude Code 会话历史（~/.claude/projects/），输出统一会话结构。

相比 agentsync 的 claude-session-continue/scripts/list_sessions.py：
- 改整文件读取为 head+tail 快扫（对 TUI 友好）。
- 修正原生标题字段名：是 aiTitle，不是 title。
- 补充提取 cwd（用于回车后 cd 到正确目录）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titles

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/")

_SKIP_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


def _extract_text(content) -> str | None:
    """从消息 content 中提取纯文本，跳过本地命令回显。"""
    if isinstance(content, str):
        t = content.strip()
        if not t:
            return None
        command_args = re.search(r"<command-args>(.*?)</command-args>", t, re.DOTALL)
        if command_args:
            args = command_args.group(1).strip()
            return args or None
        if t.startswith(_SKIP_PREFIXES):
            return None
        if re.match(r"^<\w+>.*</\w+>$", t, re.DOTALL):
            return None
        return t
    elif isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "").strip()
                if t:
                    texts.append(t)
        return " ".join(texts) if texts else None
    return None


def _parse_timestamp(value) -> float | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _entry_time(entry: dict) -> float | None:
    return _parse_timestamp(entry.get("timestamp")) or _parse_timestamp(entry.get("snapshot", {}).get("timestamp"))


def _format_display_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def _apply_effective_times(sessions: list[dict]) -> None:
    """修正批量 touch / 同步污染的 mtime。

    正常情况下文件 mtime 最符合“最近被续接/写入”的直觉；但 Syncthing、复制或
    批量元数据刷新会让一批历史会话落在同一分钟。检测到这种簇时，回退到会话
    内部最后事件时间，避免列表前排全是同一个虚假的时间。
    """
    buckets: dict[int, list[dict]] = {}
    for session in sessions:
        buckets.setdefault(int(session["file_mtime"] // 60), []).append(session)

    polluted_buckets: set[int] = set()
    for bucket, bucket_sessions in buckets.items():
        if len(bucket_sessions) < 5:
            continue
        near_ctime_count = sum(
            1
            for session in bucket_sessions
            if abs(session.get("file_ctime", session["file_mtime"]) - session["file_mtime"]) <= 120
        )
        if near_ctime_count >= max(5, len(bucket_sessions) * 2 // 3):
            polluted_buckets.add(bucket)

    for session in sessions:
        bucket = int(session["file_mtime"] // 60)
        event_time = session.get("event_time")
        effective_time = session["file_mtime"]
        time_source = "file_mtime"
        if (
            bucket in polluted_buckets
            and event_time is not None
            and abs(session["file_mtime"] - event_time) > 3600
        ):
            effective_time = event_time
            time_source = "event_time_bulk_mtime"
        session["mtime"] = effective_time
        session["display_time"] = _format_display_time(effective_time)
        session["time_source"] = time_source


def _read_head(path: str, max_lines: int = 300) -> list[dict]:
    """读取文件头部若干行，提取 cwd、首条用户消息和稍晚出现的 ai-title。"""
    entries: list[dict] = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(obj)
    except OSError:
        pass
    return entries


def _read_tail(path: str, max_bytes: int = 65536) -> list[dict]:
    """读取文件尾部若干字节，解析 JSONL 条目（用于判末轮角色 + 补抓晚出现的 ai-title）。"""
    entries: list[dict] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            offset = max(0, size - max_bytes)
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if offset > 0:
            lines = lines[1:]  # 第一行可能截断，跳过
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return entries


_INTERRUPTED_MARKER = "[Request interrupted by user]"

_LOW_VALUE_PROMPTS = {
    "继续",
    "继续吧",
    "你继续",
    "在吗",
    "在？",
    "快点",
    "快点儿",
    "快点啊",
    "好的",
    "好",
    "ok",
    "yes",
    "no",
    "不用",
    "requestinterruptedbyuser",
    "continuefromwhereyouleftoff",
    "noresponserequested",
}

_NOISE_PROMPT_PREFIXES = (
    titles.PROMPT_MARKER,
    "API Error:",
    "Base directory for this skill:",
    "No response requested.",
    "This page isn't working",
    "This page isn’t working",
    "You've hit your session limit",
    "你是 OpenConductor 的管理者 Agent",
    "你是 OpenConductor 的聊天意图解析器",
    "你将看到一批编程助手会话的摘录",
)

_DOC_COMMAND_LABELS = {
    "doc-init": "文档初始化",
    "doc-update": "会话文档复盘",
    "doc-compact": "文档整理压缩",
    "doc-audit": "文档审查",
}


def _title_line(text: str | None) -> str | None:
    if not text:
        return None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith("› ") or line.startswith("> "):
            line = line[2:].strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            continue
        return re.sub(r"\s+", " ", line)
    return None


def _normalize_title_line(line: str | None) -> str | None:
    if not line:
        return None
    line = re.sub(r"\s+", " ", line.strip())
    if not line:
        return None

    for command, label in _DOC_COMMAND_LABELS.items():
        command_match = re.fullmatch(rf"[/\$]{command}\s+@?([\w.-]+?)/?", line, flags=re.IGNORECASE)
        if command_match:
            return f"{command_match.group(1)} {label}"
        if re.fullmatch(rf"[/\$]{command}", line, flags=re.IGNORECASE):
            return label

    line = re.sub(r"^(?:@[\w.-]+/?\s+)+", "", line).strip()
    return line or None


def _is_low_value_title(text: str | None) -> bool:
    line = _title_line(text)
    if not line:
        return True
    if line in {"...", "…"}:
        return True
    if line.startswith(("{", "[")):
        return True  # 结构化 JSON/数组片段；截断或被 pretty-print 折行后未必能 fullmatch 闭合括号
    compact = re.sub(r"[\s,，。.!！?？:：;；'\"`~～…\[\]()（）{}<>《》]+", "", line).lower()
    if compact in _LOW_VALUE_PROMPTS:
        return True
    if compact.startswith(("你测试了吗", "测试了吗", "快点继续")):
        return True
    if len(compact) <= 8 and compact.startswith(("继续", "快点")):
        return True
    return any(line.startswith(prefix) for prefix in _NOISE_PROMPT_PREFIXES)


def _short_title(text: str) -> str:
    line = _normalize_title_line(_title_line(text)) or ""
    return line[:60] + "…" if len(line) > 60 else line


def _choose_claude_fallback_title(candidates: list[tuple[str, str | None]]) -> str:
    scored: list[tuple[int, str]] = []
    for source, text in candidates:
        if _is_low_value_title(text):
            continue
        title = _short_title(str(text))
        if not title:
            continue
        score = 10
        if source == "last_prompt":
            score = 40
        elif source == "last_user":
            score = 35
        elif source == "first_user":
            score = 25
        elif source == "last_agent":
            score = 20
        scored.append((score, title))

    if scored:
        return max(scored, key=lambda item: item[0])[1]
    return "(仅本地命令)"


def _shorten_cwd(cwd: str) -> str:
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def _build_session_info(fpath: str, proj: str) -> dict:
    session_id = os.path.basename(fpath).replace(".jsonl", "")
    head_entries = _read_head(fpath)
    tail_entries = _read_tail(fpath)

    cwd = None
    first_user_msg = None
    last_prompt = None
    ai_title = None
    title_candidates: list[tuple[str, str | None]] = []

    for e in head_entries:
        if cwd is None and e.get("cwd"):
            cwd = e.get("cwd")
        if e.get("type") == "ai-title":
            t = e.get("aiTitle")
            if t and t != "?":
                ai_title = t
        if e.get("type") == "user" and first_user_msg is None:
            text = _extract_text(e.get("message", {}).get("content", ""))
            if text:
                first_user_msg = text
                title_candidates.append(("first_user", text))
        if e.get("type") == "last-prompt" and e.get("lastPrompt"):
            last_prompt = e.get("lastPrompt")
            title_candidates.append(("last_prompt", last_prompt))

    last_user_msg = None
    last_agent_msg = None
    last_was_user = None
    event_time = None

    for e in tail_entries:
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        if t == "ai-title":
            title = e.get("aiTitle")
            if title and title != "?":
                ai_title = title  # 尾部出现的标题更新，覆盖头部
        elif t == "last-prompt" and e.get("lastPrompt"):
            last_prompt = e.get("lastPrompt")
            title_candidates.append(("last_prompt", last_prompt))
        elif t == "user":
            text = _extract_text(e.get("message", {}).get("content", ""))
            if text == _INTERRUPTED_MARKER:
                last_was_user = "aborted"  # 用户主动中断当前轮次，不是真实用户消息
            elif text:
                last_user_msg = text
                title_candidates.append(("last_user", text))
                last_was_user = True
        elif t == "assistant":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip():
                        last_agent_msg = part["text"]
                        title_candidates.append(("last_agent", last_agent_msg))
                        last_was_user = False
                        break

    stat = os.stat(fpath)
    for e in head_entries:
        entry_time = _entry_time(e)
        if entry_time is not None and (event_time is None or entry_time > event_time):
            event_time = entry_time
    session_time = stat.st_mtime
    fallback = _choose_claude_fallback_title(title_candidates)

    if last_was_user == "aborted":
        status_tag = titles.STATUS_ABORTED
    elif last_was_user is True:
        status_tag = titles.STATUS_PENDING
    elif last_was_user is False:
        status_tag = titles.STATUS_DONE
    else:
        status_tag = titles.STATUS_NONE

    return {
        "source": "claude",
        "id": session_id,
        "short_id": session_id[:8],
        "cwd": cwd or "",
        "cwd_display": _shorten_cwd(cwd or ""),
        "mtime": session_time,
        "display_time": _format_display_time(session_time),
        "event_time": event_time,
        "file_mtime": stat.st_mtime,
        "file_ctime": stat.st_ctime,
        "size_bytes": stat.st_size,
        "size_kb": round(stat.st_size / 1024, 1),
        "native_title": ai_title,
        "fallback_title": fallback,
        "status_tag": status_tag,
        "first_user_msg": (first_user_msg or "")[:300],
        "last_user_msg": (last_user_msg or "")[:300],
        "last_agent_msg": (last_agent_msg or "")[:300],
        "path": fpath,
    }


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描所有项目下的 Claude Code 会话，返回统一结构列表，按 mtime 降序。"""
    if not os.path.isdir(PROJECTS_DIR):
        return []

    candidates: list[tuple[str, str]] = []
    for proj in os.listdir(PROJECTS_DIR):
        proj_base = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_base):
            continue
        for fname in os.listdir(proj_base):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_base, fname)
            candidates.append((fpath, proj))

    results: list[dict] = []
    for fpath, proj in candidates:
        try:
            info = _build_session_info(fpath, proj)
        except OSError:
            continue
        if not info["first_user_msg"] or info["fallback_title"] == "(仅本地命令)":
            continue  # 无用户消息的空会话
        if info["first_user_msg"].startswith(titles.PROMPT_MARKER):
            continue  # sc 自己生成标题留下的噪音会话，跳过
        if info["cwd"] and not os.path.isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)

    _apply_effective_times(results)
    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


if __name__ == "__main__":
    import sys

    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Claude 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {s['status_tag']:<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
