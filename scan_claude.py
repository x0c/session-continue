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
from models import ConversationMessage, effective_session_time, format_message_time

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


_format_display_time = format_message_time


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
    session_time, time_source = effective_session_time(stat.st_mtime, event_time)
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
        "time_source": time_source,
        "event_time": event_time,
        "file_mtime": stat.st_mtime,
        "size_bytes": stat.st_size,
        "size_kb": round(stat.st_size / 1024, 1),
        "native_title": ai_title,
        "fallback_title": fallback,
        "status_tag": status_tag,
        "live": False,  # scan_sessions 统一按 _live_session_ids() 回填
        "pid": None,  # 同上，运行中会话的进程号
        "first_user_msg": (first_user_msg or "")[:300],
        "last_user_msg": (last_user_msg or "")[:300],
        "last_agent_msg": (last_agent_msg or "")[:300],
        "path": fpath,
    }


SESSIONS_DIR = os.path.expanduser("~/.claude/sessions/")


def _live_session_ids() -> dict[str, int]:
    """扫描 ~/.claude/sessions/{pid}.json，返回进程仍存活的 sessionId -> pid 映射。

    与 active-claude-sessions skill 同一判活思路：pid 文件是 Claude Code 自己
    维护的运行时状态，os.kill(pid, 0) 能确认进程是否还真实存在（而不是残留的
    陈旧文件）。文件名本身就是 pid，判活的同时顺手记下来，供 Agent 接口把
    「哪个会话在跑」精确到进程号。
    """
    live_ids: dict[str, int] = {}
    if not os.path.isdir(SESSIONS_DIR):
        return live_ids
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            pid = int(fname[: -len(".json")])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fname)) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        session_id = data.get("sessionId")
        if session_id:
            live_ids[session_id] = pid
    return live_ids


def _peek_head_meta(path: str, max_lines: int = 40) -> tuple[str | None, str | None]:
    """只读文件头部少量行，廉价探出 cwd 和首条用户消息，供跳过前置过滤用。

    对撞上首屏 1s 硬指标的两个根因做提前拦截：自产噪音会话（后台标题生成
    调 claude/codex 留下的、以 PROMPT_MARKER 开头的会话）和 cwd 已删的会话，
    不必等 _build_session_info 读完整 300 行头 + 64KB 尾才发现能丢弃。
    只要拿到 cwd 和首条用户消息就早停；两者任一没探到时上层不跳过，照常走
    完整解析（避免误杀头部很长的真实会话）。
    """
    cwd: str | None = None
    first_user: str | None = None
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
                if cwd is None and obj.get("cwd"):
                    cwd = obj.get("cwd")
                if obj.get("type") == "user" and first_user is None:
                    text = _extract_text(obj.get("message", {}).get("content", ""))
                    if text:
                        first_user = text
                if cwd is not None and first_user is not None:
                    break
    except OSError:
        pass
    return cwd, first_user


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描所有项目下的 Claude Code 会话，返回统一结构列表，按 mtime 降序。

    历史会话可能有成百上千个，但调用方只要最近 limit 条。真正耗时的
    _build_session_info 会读取整个文件头尾并解析 JSONL，所以先用一次廉价的
    os.stat 按文件 mtime 排好序，只对最可能入选的候选文件做完整解析，凑够
    limit 条有效结果就停止。

    首屏必须 ≤1s（见 AGENTS.md 验证要求），这里做两项针对性优化，改动前后
    结果字节级一致（已用真实会话数据核验 id 顺序、兜底标题、原生标题）：
    - cwd 判活按 cwd 记忆化（同一次扫描里大量会话共享极少数 cwd，重复
      os.path.isdir 在同步/网络目录上很慢，是首屏卡顿主因之一）；
    - 完整解析前先用 _peek_head_meta 廉价探测，提前跳过自产噪音会话和
      cwd 已删的会话，避免整文件解析后才发现能丢弃（另一大主因）。
    """
    if not os.path.isdir(PROJECTS_DIR):
        return []

    live_ids = _live_session_ids()
    candidates: list[tuple[float, str, str]] = []
    for proj in os.listdir(PROJECTS_DIR):
        proj_base = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_base):
            continue
        for fname in os.listdir(proj_base):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_base, fname)
            try:
                mtime = os.stat(fpath).st_mtime
            except OSError:
                continue
            candidates.append((mtime, fpath, proj))

    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    for mtime, fpath, proj in candidates:
        if len(results) >= limit:
            break

        peek_cwd, peek_first_user = _peek_head_meta(fpath)
        if peek_first_user is not None and peek_first_user.startswith(titles.PROMPT_MARKER):
            continue  # 廉价探测已确认是自产噪音会话，跳过整文件解析
        if peek_cwd and not cached_isdir(peek_cwd):
            continue  # 廉价探测已确认 cwd 不存在，跳过整文件解析

        try:
            info = _build_session_info(fpath, proj)
        except OSError:
            continue
        if not info["first_user_msg"] or info["fallback_title"] == "(仅本地命令)":
            continue  # 无用户消息的空会话
        if info["first_user_msg"].startswith(titles.PROMPT_MARKER):
            continue  # sc 自己生成标题留下的噪音会话，跳过（廉价探测失手时的兜底）
        if info["cwd"] and not cached_isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        info["live"] = info["id"] in live_ids
        info["pid"] = live_ids.get(info["id"])
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真实用户消息和 Claude 的每段文本回复。

    注意：一次 assistant 轮次里 thinking/text/tool_use 各是独立的 JSONL 行，且共享同一个
    `stop_reason`（哪怕这行本身是纯文本、后面还接着工具调用，`stop_reason` 也是
    `tool_use`）。之前按 `stop_reason in (None, "end_turn")` 过滤会把工具调用前后夹带的文本
    说明整段丢掉，只保留触发了 `stop_reason=None` 分支的历史遗留格式和轮次末尾无工具调用
    的纯文本；这里只按内容是否为空文本过滤，不再看 `stop_reason`。
    """
    messages: list[ConversationMessage] = []
    pending_legacy_answer: str | None = None
    pending_legacy_ts: float | None = None

    def flush_legacy_answer() -> None:
        nonlocal pending_legacy_answer, pending_legacy_ts
        if pending_legacy_answer and (
            not messages or messages[-1].role != "assistant" or messages[-1].text != pending_legacy_answer
        ):
            messages.append(ConversationMessage("assistant", pending_legacy_answer, pending_legacy_ts))
        pending_legacy_answer = None
        pending_legacy_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("isMeta") or entry.get("isSidechain"):
                    continue

                entry_type = entry.get("type")
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue

                if entry_type == "user":
                    origin = entry.get("origin")
                    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                    if origin_kind not in (None, "human"):
                        # task-notification 等系统注入事件也挂在 user 轮次下，但不是真人输入。
                        # 预览只展示 Agent 和真人的对话，这类系统事件价值很低，整条丢弃，不展示。
                        continue
                    text = _extract_text(message.get("content", ""))
                    if text and text != _INTERRUPTED_MARKER:
                        flush_legacy_answer()
                        messages.append(ConversationMessage("user", text, _entry_time(entry)))
                    continue

                if entry_type != "assistant":
                    continue
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue
                text_parts = [
                    str(part.get("text", "")).strip()
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip()
                ]
                if text_parts:
                    text = "\n\n".join(text_parts)
                    if message.get("stop_reason") is None:
                        pending_legacy_answer = text
                        pending_legacy_ts = _entry_time(entry)
                    elif not messages or messages[-1].role != "assistant" or messages[-1].text != text:
                        pending_legacy_answer = None
                        pending_legacy_ts = None
                        messages.append(ConversationMessage("assistant", text, _entry_time(entry)))
    except OSError:
        return []
    flush_legacy_answer()
    return messages


if __name__ == "__main__":
    import sys

    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Claude 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'进行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
