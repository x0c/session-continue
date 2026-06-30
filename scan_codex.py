#!/usr/bin/env python3
"""扫描 Codex 会话历史（~/.codex/sessions/），输出统一会话结构。

移植自 agentsync 的 codex-session-continue/scripts/list_sessions.py，
去掉了 CLI/表格输出，只保留 scan_sessions() 供 sc.py 消费。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titles
from models import ConversationMessage

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
SESSION_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")


def _load_index() -> dict[str, str]:
    """加载 session_index.jsonl，返回 id -> thread_name 映射。"""
    index: dict[str, str] = {}
    if not os.path.isfile(SESSION_INDEX):
        return index
    try:
        with open(SESSION_INDEX, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    sid = obj.get("id")
                    name = obj.get("thread_name")
                    if sid and name:
                        index[sid] = name
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return index


def _find_all_session_files() -> list[str]:
    """递归扫描 ~/.codex/sessions/ 下所有 .jsonl 文件，按文件名（时间戳）降序排列。"""
    files: list[str] = []
    if not os.path.isdir(SESSIONS_DIR):
        return files
    for root, dirs, fnames in os.walk(SESSIONS_DIR):
        dirs.sort()
        for fname in fnames:
            if fname.endswith(".jsonl"):
                files.append(os.path.join(root, fname))
    files.sort(reverse=True)
    return files


def _extract_uuid_from_filename(path: str) -> str | None:
    """从文件名中提取 UUID。格式: rollout-YYYY-MM-DDThh-mm-ss-<UUID>.jsonl"""
    fname = os.path.basename(path)
    m = re.search(
        r"rollout-[\d-]+T[\d-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
        fname,
    )
    return m.group(1) if m else None


def _extract_datetime_from_filename(path: str) -> datetime | None:
    """从文件名提取时间戳。格式: rollout-YYYY-MM-DDThh-mm-ss-..."""
    fname = os.path.basename(path)
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-", fname)
    if m:
        try:
            return datetime(*[int(x) for x in m.groups()])
        except ValueError:
            pass
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
    payload = entry.get("payload", {})
    return _parse_timestamp(entry.get("timestamp")) or _parse_timestamp(payload.get("timestamp"))


def _format_display_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def _read_session_head(path: str, max_lines: int = 30) -> list[dict]:
    """逐行读取文件头部，找到 session_meta 和第一条 user_message 后停止。"""
    entries: list[dict] = []
    found_meta = False
    found_user = False
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
                    entries.append(obj)
                    t = obj.get("type")
                    pt = obj.get("payload", {}).get("type", "")
                    if t == "session_meta":
                        found_meta = True
                    if t == "event_msg" and pt == "user_message":
                        found_user = True
                    if found_meta and found_user:
                        break
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return entries


def _read_session_tail(path: str, max_bytes: int = 8192) -> list[dict]:
    """读取文件尾部若干字节，解析 JSONL 条目。"""
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


def _shorten_cwd(cwd: str) -> str:
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def _status_tag(last_event_type: str | None) -> str:
    """末轮状态判定，与 scan_claude.py 共用 titles.py 里的统一枚举。"""
    if last_event_type == "turn_aborted":
        return titles.STATUS_ABORTED
    if last_event_type == "user_message":
        return titles.STATUS_PENDING
    if last_event_type in ("task_complete", "agent_message"):
        return titles.STATUS_DONE
    return titles.STATUS_NONE


def _build_session_info(path: str, index: dict[str, str]) -> dict | None:
    """从一个 session 文件中提取统一结构。"""
    uuid = _extract_uuid_from_filename(path)
    if not uuid:
        return None

    dt = _extract_datetime_from_filename(path)
    head_entries = _read_session_head(path)
    tail_entries = _read_session_tail(path)

    cwd = None
    first_user_msg = None
    last_user_msg = None
    last_agent_msg = None
    last_event_type = None
    event_time = None

    for e in head_entries:
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        payload = e.get("payload", {})
        pt = payload.get("type", "")
        if t == "session_meta" and cwd is None:
            cwd = payload.get("cwd")
        if t == "event_msg" and pt == "user_message" and first_user_msg is None:
            first_user_msg = payload.get("message", "")

    for e in tail_entries:
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        payload = e.get("payload", {})
        pt = payload.get("type", "")
        if t == "event_msg":
            if pt == "user_message":
                last_user_msg = payload.get("message", "")
                last_event_type = "user_message"
            elif pt == "agent_message":
                last_agent_msg = payload.get("message", "")
                last_event_type = "agent_message"
            elif pt == "task_complete":
                msg = payload.get("last_agent_message")
                if msg:
                    last_agent_msg = msg
                last_event_type = "task_complete"
            elif pt == "turn_aborted":
                last_event_type = "turn_aborted"

    mtime = os.path.getmtime(path)
    size_bytes = os.path.getsize(path)
    size_kb = size_bytes / 1024
    session_time = mtime
    fallback = (first_user_msg or "").split("\n")[0].strip()
    if len(fallback) > 60:
        fallback = fallback[:60] + "…"
    if not fallback:
        fallback = "(无消息)"

    return {
        "source": "codex",
        "id": uuid,
        "short_id": uuid[:8],
        "cwd": cwd or "",
        "cwd_display": _shorten_cwd(cwd or ""),
        "mtime": session_time,
        "display_time": _format_display_time(session_time),
        "event_time": event_time or (dt.timestamp() if dt else None),
        "file_mtime": mtime,
        "size_bytes": size_bytes,
        "size_kb": round(size_kb, 1),
        "native_title": index.get(uuid),
        "fallback_title": fallback,
        "status_tag": _status_tag(last_event_type),
        "first_user_msg": (first_user_msg or "")[:300],
        "last_user_msg": (last_user_msg or "")[:300],
        "last_agent_msg": (last_agent_msg or "")[:300],
        "path": path,
    }


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描 Codex 会话，返回统一结构列表，按 mtime 降序。"""
    index = _load_index()
    all_files = _find_all_session_files()

    results: list[dict] = []
    for path in all_files:
        try:
            info = _build_session_info(path, index)
        except OSError:
            continue
        if info is None:
            continue
        if not info["first_user_msg"] or info["fallback_title"] == "(无消息)":
            continue  # 无用户消息的空会话
        if info["cwd"] and not os.path.isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真实用户消息和 Codex 每轮最终答复。"""
    messages: list[ConversationMessage] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload", {})
                if not isinstance(payload, dict):
                    continue

                payload_type = payload.get("type")
                if payload_type == "user_message":
                    text = str(payload.get("message", "")).strip()
                    if text:
                        messages.append(ConversationMessage("user", text))
                elif payload_type == "agent_message" and payload.get("phase") in (None, "final_answer"):
                    text = str(payload.get("message", "")).strip()
                    if text and (not messages or messages[-1].role != "assistant" or messages[-1].text != text):
                        messages.append(ConversationMessage("assistant", text))
                elif payload_type == "task_complete":
                    text = str(payload.get("last_agent_message", "")).strip()
                    if text and (not messages or messages[-1].role != "assistant" or messages[-1].text != text):
                        messages.append(ConversationMessage("assistant", text))
    except OSError:
        return []
    return messages


if __name__ == "__main__":
    import sys

    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Codex 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {s['status_tag']:<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
