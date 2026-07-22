#!/usr/bin/env python3
"""扫描 Kimi Code CLI 会话历史（~/.kimi-code/sessions/），输出统一会话结构。

Kimi Code 的会话按「工作区 / 会话」两级目录存放：

    ~/.kimi-code/sessions/<workspace_id>/<session_id>/
        state.json                  会话元数据（标题、工作目录、创建/更新时间、最后一条 prompt）
        agents/main/wire.jsonl      主 agent 的对话流水（协议事件逐行 JSON）
        agents/<other>/wire.jsonl   子 agent 的旁路对话，扫描与预览一律忽略

元数据优先取 state.json（小而权威）；用户/助手正文只能从 wire.jsonl 里解析。
wire.jsonl 里混着体量很大的系统提示（config.update）和工具快照（llm.tools_snapshot），
逐行 json.loads 会很慢，这里先按类型子串廉价过滤，只解析真正承载对话的两类事件：

- 用户消息：type == "context.append_message" 且 message.role == "user"，
  正文在 message.content 里 type=="text" 的分片；origin.kind 非 "user" 的系统注入事件丢弃。
- 助手正文：type == "context.append_loop_event" 且 event.type == "content.part"
  且 event.part.type == "text"（part.type == "think" 是思考过程，跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pickup import titles
from pickup.models import ConversationMessage, effective_session_time, format_message_time
from pickup.scan.common import is_ephemeral_agent_cwd
from pickup.scan.common import live_pids_by_process_name
from pickup.scan.common import parse_timestamp as _parse_iso
from pickup.scan.common import shorten_cwd as _shorten_cwd

KIMI_HOME = os.path.expanduser("~/.kimi-code")
SESSIONS_DIR = os.path.join(KIMI_HOME, "sessions")

# 只解析承载对话的事件行，跳过体量很大的系统提示 / 工具快照，避免整段 json.loads。
# 用带引号的类型值（不含冒号）做子串匹配，兼容紧凑与带空格两种 JSON 写法。
_USER_EVENT_MARKER = '"context.append_message"'
_LOOP_EVENT_MARKER = '"context.append_loop_event"'


def _event_time(entry: dict) -> float | None:
    """wire.jsonl 每行的 time 是毫秒 epoch，转成秒。"""
    t = entry.get("time")
    if isinstance(t, (int, float)):
        return t / 1000
    return None



def _text_from_parts(parts) -> str:
    """从 message.content 分片列表里拼接 type=="text" 的正文。"""
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            t = str(part.get("text") or "").strip()
            if t:
                texts.append(t)
    return "\n\n".join(texts)


def _user_text(entry: dict) -> str | None:
    """从 context.append_message 事件里取真人用户正文；系统注入事件返回 None。"""
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    origin = message.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    # origin.kind 为 "user" 才是真人输入；task-notification 等系统事件也走 user 轮次，丢弃。
    if origin_kind not in (None, "user"):
        return None
    text = _text_from_parts(message.get("content"))
    return text or None


def _assistant_part_text(entry: dict) -> str | None:
    """从 context.append_loop_event 的 content.part 事件里取助手文本；思考分片返回 None。"""
    event = entry.get("event")
    if not isinstance(event, dict) or event.get("type") != "content.part":
        return None
    part = event.get("part")
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = str(part.get("text") or "").strip()
    return text or None


def _iter_message_entries(lines):
    """从原始文本行里过滤并解析出对话事件（跳过系统提示 / 工具快照等大行）。"""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _USER_EVENT_MARKER not in line and _LOOP_EVENT_MARKER not in line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _read_head_lines(path: str, max_lines: int = 400) -> list[str]:
    lines: list[str] = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line)
    except OSError:
        pass
    return lines


def _read_tail_lines(path: str, max_bytes: int = 131072) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            offset = max(0, size - max_bytes)
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 首行可能被截断，丢弃
    return lines


def _wire_path(session_dir: str) -> str:
    return os.path.join(session_dir, "agents", "main", "wire.jsonl")


def _load_state(session_dir: str) -> dict:
    try:
        with open(os.path.join(session_dir, "state.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_session_info(session_dir: str, session_id: str) -> dict | None:
    state = _load_state(session_dir)
    wire_path = _wire_path(session_dir)
    try:
        stat = os.stat(wire_path)
    except OSError:
        return None

    cwd = str(state.get("workDir") or "")
    native_title = state.get("title") or None
    updated_at = _parse_iso(state.get("updatedAt"))

    # 头部取首条真人用户消息；尾部取末条用户 / 助手消息并判定末轮角色。
    first_user_msg = None
    for entry in _iter_message_entries(_read_head_lines(wire_path)):
        text = _user_text(entry)
        if text:
            first_user_msg = text
            break

    last_user_msg = None
    last_agent_msg = None
    last_role = None
    event_time = None
    pending_assistant: list[str] = []

    def flush_assistant():
        nonlocal last_agent_msg
        if pending_assistant:
            last_agent_msg = "\n\n".join(pending_assistant)
            pending_assistant.clear()

    for entry in _iter_message_entries(_read_tail_lines(wire_path)):
        t = _event_time(entry)
        if t is not None:
            event_time = t
        user_text = _user_text(entry)
        if user_text is not None:
            flush_assistant()
            last_user_msg = user_text
            last_role = "user"
            continue
        agent_text = _assistant_part_text(entry)
        if agent_text is not None:
            if last_role != "assistant":
                pending_assistant.clear()
            pending_assistant.append(agent_text)
            last_role = "assistant"
    flush_assistant()

    file_mtime = updated_at if updated_at is not None else stat.st_mtime
    session_time, time_source = effective_session_time(file_mtime, event_time)

    if last_role == "user":
        status_tag = titles.STATUS_PENDING
    elif last_role == "assistant":
        status_tag = titles.STATUS_DONE
    else:
        status_tag = titles.STATUS_NONE

    # 兜底标题：原生标题 > 首条用户消息 > 最后一条 prompt。
    # candidate 可能是纯空白（如 " "），strip() 后为空串时 splitlines() 返回空列表，
    # 取 [0] 会 IndexError；先判空再取首行，纯空白候选按"无标题"处理，尝试下一个来源。
    fallback = ""
    for candidate in (native_title, first_user_msg, state.get("lastPrompt")):
        stripped = str(candidate or "").strip()
        lines = stripped.splitlines()
        line = lines[0].strip() if lines else ""
        if line:
            fallback = line[:60] + "…" if len(line) > 60 else line
            break
    if not first_user_msg and not native_title and not fallback:
        return None  # 空会话（刚创建、还没任何用户消息），无展示价值

    return {
        "source": "kimi",
        "id": session_id,
        "short_id": session_id.replace("session_", "")[:12],
        "cwd": cwd,
        "cwd_display": _shorten_cwd(cwd),
        "mtime": session_time,
        "display_time": format_message_time(session_time),
        "time_source": time_source,
        "event_time": event_time,
        "file_mtime": file_mtime,
        "size_bytes": stat.st_size,
        "size_kb": round(stat.st_size / 1024, 1),
        "native_title": native_title,
        "fallback_title": fallback or "(无消息)",
        "status_tag": status_tag,
        "live": False,  # scan_sessions 统一按 live_pids_by_process_name() 回填
        "pid": None,
        "first_user_msg": (first_user_msg or "")[:300],
        "last_user_msg": (last_user_msg or "")[:300],
        "last_agent_msg": (last_agent_msg or "")[:300],
        "path": wire_path,
    }


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描所有 Kimi Code 会话，返回统一结构列表，按 mtime 降序。

    先用一次廉价的 os.stat（按 wire.jsonl 文件 mtime）排序，只对最可能入选的
    候选做完整解析，凑够 limit 条有效结果就停止；首屏 ≤1s 预算见 AGENTS.md。
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []

    candidates: list[tuple[float, str, str]] = []
    for workspace_id in os.listdir(SESSIONS_DIR):
        workspace_dir = os.path.join(SESSIONS_DIR, workspace_id)
        if not os.path.isdir(workspace_dir):
            continue
        for session_id in os.listdir(workspace_dir):
            session_dir = os.path.join(workspace_dir, session_id)
            if not os.path.isdir(session_dir):
                continue
            try:
                mtime = os.stat(_wire_path(session_dir)).st_mtime
            except OSError:
                continue
            candidates.append((mtime, session_dir, session_id))

    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    for _, session_dir, session_id in candidates:
        if len(results) >= limit:
            break
        info = _build_session_info(session_dir, session_id)
        if info is None:
            continue
        if is_ephemeral_agent_cwd(info["cwd"]):
            continue  # OpenConductor 管家临时 cwd，目录复活会刷屏
        if info["cwd"] and not cached_isdir(info["cwd"]):
            continue  # 工作目录已删除，无法原生恢复
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]

    if not results:
        return results  # 无会话时跳过 pgrep 子进程，省下首屏预算里最贵的一笔

    live_by_cwd = live_pids_by_process_name("kimi-code")
    for info in results:  # 已按 mtime 降序，同 cwd 只把最新一条标记存活
        cwd = info.get("cwd") or ""
        pid = live_by_cwd.pop(os.path.realpath(cwd), None) if cwd else None
        if pid is not None:
            info["live"] = True
            info["pid"] = pid
    return results


def delete_session(path: str) -> None:
    """彻底删除单个 Kimi Code 会话，不可恢复。

    `path` 是 `_wire_path()` 返回的 `.../<workspace_id>/<session_id>/agents/main/wire.jsonl`，
    只删这一个文件会留下 state.json、agents/ 及其他子 agent 目录；必须整个会话目录
    （wire.jsonl 往上数三级）一起删。
    """
    session_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir)


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真人用户消息和助手每轮文本回复。

    助手一轮里可能穿插思考、多段文本和工具调用，思考（part.type=="think"）跳过，
    连续的文本分片合并成一条助手消息，遇到下一条用户消息即断开成新一轮。
    """
    messages: list[ConversationMessage] = []
    pending_assistant: list[str] = []
    pending_ts: float | None = None

    def flush_assistant():
        nonlocal pending_ts
        if pending_assistant:
            messages.append(ConversationMessage("assistant", "\n\n".join(pending_assistant), pending_ts))
            pending_assistant.clear()
            pending_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for entry in _iter_message_entries(file):
                user_text = _user_text(entry)
                if user_text is not None:
                    flush_assistant()
                    messages.append(ConversationMessage("user", user_text, _event_time(entry)))
                    continue
                agent_text = _assistant_part_text(entry)
                if agent_text is not None:
                    if not pending_assistant:
                        pending_ts = _event_time(entry)
                    pending_assistant.append(agent_text)
    except OSError:
        return []
    flush_assistant()
    return messages


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Kimi Code 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r} "
            f"status={s['status_tag']!r}"
        )
