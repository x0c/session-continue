#!/usr/bin/env python3
"""扫描 Cursor Agent CLI 会话历史（~/.cursor/chats/），输出统一会话结构。

Cursor CLI 的会话按「工作区哈希 / 会话 UUID」两级目录存放：

    ~/.cursor/chats/<workspace_hash>/<chat_id>/
        meta.json              标题、cwd、createdAtMs/updatedAtMs、hasConversation
        prompt_history.json    用户输入列表（可选；**最新在前**）
        store.db               完整对话（blobs 内容寻址；列表扫描不读，预览按需读）

列表扫描只碰 meta.json / prompt_history.json，保证首屏轻量。完整对话由
load_conversation() 只读打开 store.db，解析 JSON blob 里 role=user/assistant
的正文；二进制 DAG blob 跳过。用户正文优先取 <user_query>…</user_query>。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pickup import titles
from pickup.models import ConversationMessage, effective_session_time, format_message_time
from pickup.scan.common import live_processes, process_command_line
from pickup.scan.common import shorten_cwd as _shorten_cwd

CHATS_DIR = os.path.expanduser("~/.cursor/chats")

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# Cursor CLI：`agent --resume <uuid>` / `--resume=<uuid>`；`-1` 表示续最近一条，无法精确绑定。
_RESUME_ID_RE = re.compile(
    r"--resume(?:=|\s+)(?P<id>-1|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _prompt_history(chat_dir: str) -> list[str]:
    """返回 newest-first 的用户 prompt 列表；缺失或损坏时为空。"""
    data = _read_json(os.path.join(chat_dir, "prompt_history.json"))
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item or "").strip()]


def _ms_to_epoch(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000.0
    return None


def _fallback_title(native_title: str | None, first_user: str, last_user: str) -> str:
    for candidate in (native_title, first_user, last_user):
        stripped = str(candidate or "").strip()
        line = stripped.splitlines()[0].strip() if stripped.splitlines() else ""
        if line:
            return line[:60] + "…" if len(line) > 60 else line
    return "(无消息)"


def _chat_size_bytes(chat_dir: str) -> int:
    total = 0
    for name in ("meta.json", "prompt_history.json", "store.db"):
        try:
            total += os.path.getsize(os.path.join(chat_dir, name))
        except OSError:
            continue
    return total


def _build_session_info(chat_dir: str, chat_id: str) -> dict | None:
    meta = _read_json(os.path.join(chat_dir, "meta.json"))
    if not isinstance(meta, dict):
        return None
    if meta.get("hasConversation") is False:
        return None

    cwd = str(meta.get("cwd") or "").strip()
    native_title = str(meta.get("title") or "").strip() or None
    prompts = _prompt_history(chat_dir)
    # prompt_history 最新在前
    last_user_msg = prompts[0] if prompts else ""
    first_user_msg = prompts[-1] if prompts else ""

    if not first_user_msg and not native_title:
        return None

    updated = _ms_to_epoch(meta.get("updatedAtMs"))
    created = _ms_to_epoch(meta.get("createdAtMs"))
    try:
        file_mtime = os.path.getmtime(os.path.join(chat_dir, "meta.json"))
    except OSError:
        try:
            file_mtime = os.path.getmtime(chat_dir)
        except OSError:
            return None
    event_time = updated or created
    session_time, time_source = effective_session_time(file_mtime, event_time)

    if last_user_msg and not native_title:
        status_tag = titles.STATUS_PENDING
    elif native_title or last_user_msg:
        status_tag = titles.STATUS_DONE
    else:
        status_tag = titles.STATUS_NONE

    size_bytes = _chat_size_bytes(chat_dir)
    store_db = os.path.join(chat_dir, "store.db")
    history_path = store_db if os.path.isfile(store_db) else chat_dir
    return {
        "source": "cursor",
        "id": chat_id,
        "short_id": chat_id.replace("-", "")[:12],
        "cwd": cwd,
        "cwd_display": _shorten_cwd(cwd) if cwd else "",
        "mtime": session_time,
        "display_time": format_message_time(session_time),
        "time_source": time_source,
        "event_time": event_time,
        "file_mtime": file_mtime,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "native_title": native_title,
        "fallback_title": _fallback_title(native_title, first_user_msg, last_user_msg),
        "status_tag": status_tag,
        "live": False,
        "pid": None,
        "first_user_msg": first_user_msg[:300],
        "last_user_msg": last_user_msg[:300],
        "last_agent_msg": "",
        "path": history_path,
    }


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描 Cursor CLI 会话，按 mtime 降序；只读 meta/prompt_history，不打开 store.db。"""
    if not os.path.isdir(CHATS_DIR):
        return []

    candidates: list[tuple[float, str, str]] = []
    try:
        workspaces = os.listdir(CHATS_DIR)
    except OSError:
        return []
    for workspace_id in workspaces:
        workspace_dir = os.path.join(CHATS_DIR, workspace_id)
        if not os.path.isdir(workspace_dir):
            continue
        try:
            chat_ids = os.listdir(workspace_dir)
        except OSError:
            continue
        for chat_id in chat_ids:
            chat_dir = os.path.join(workspace_dir, chat_id)
            if not os.path.isdir(chat_dir):
                continue
            meta_path = os.path.join(chat_dir, "meta.json")
            try:
                mtime = os.stat(meta_path).st_mtime
            except OSError:
                continue
            candidates.append((mtime, chat_dir, chat_id))

    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    for _, chat_dir, chat_id in candidates:
        if len(results) >= limit:
            break
        info = _build_session_info(chat_dir, chat_id)
        if info is None:
            continue
        if info["cwd"] and not cached_isdir(info["cwd"]):
            continue
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]
    if not results:
        return results

    _apply_live_flags(results)
    return results


def _resume_id_from_cmdline(cmdline: str) -> str | None:
    """从 agent 命令行解析 `--resume <chatId>`；无精确 ID（含 `-1`）时返回 None。"""
    match = _RESUME_ID_RE.search(cmdline or "")
    if not match:
        return None
    resume_id = match.group("id")
    if resume_id == "-1":
        return None
    return resume_id


def _apply_live_flags(sessions: list[dict]) -> None:
    """给 Cursor 会话列表就地标注 live/pid。

    同一工作目录常会同时跑多个 `agent`（旧会话 `--resume` + 跨助手接力新建）。
    若仍按「cwd → 单个 pid」折叠，新接续会话的标题会绑到旧进程的保活画面，
    表现为侧边栏是新会话、右栏却是另一个旧会话。

    绑定优先级：
    1. 命令行带 `--resume <chatId>` 的进程，精确挂到对应会话；
    2. 其余无 resume 的进程，按 cwd 挂到尚未标记、且 mtime 最新的会话。
    """
    by_id = {str(session.get("id") or ""): session for session in sessions}
    unmatched_by_cwd: dict[str, list[int]] = {}

    for pid, cwd in live_processes("agent"):
        resume_id = _resume_id_from_cmdline(process_command_line(pid))
        if resume_id and resume_id in by_id:
            session = by_id[resume_id]
            if not session.get("live"):
                session["live"] = True
                session["pid"] = pid
            continue
        unmatched_by_cwd.setdefault(cwd, []).append(pid)

    if not unmatched_by_cwd:
        return

    candidates_by_cwd: dict[str, list[dict]] = {}
    for session in sessions:
        if session.get("live"):
            continue
        cwd = str(session.get("cwd") or "")
        if not cwd:
            continue
        candidates_by_cwd.setdefault(os.path.realpath(cwd), []).append(session)

    for cwd, pids in unmatched_by_cwd.items():
        candidates = candidates_by_cwd.get(cwd) or []
        # sessions 已按 mtime 降序；同 cwd 候选保持该顺序，逐个消费未绑定进程。
        for pid, session in zip(pids, candidates):
            session["live"] = True
            session["pid"] = pid


def _text_from_content(content) -> str:
    """把 message.content（字符串或 type==text 分片列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("text", None):
            text = str(part.get("text") or "")
            if text:
                parts.append(text)
        elif isinstance(part, str) and part:
            parts.append(part)
    return "\n".join(parts)


def _user_text_from_blob(obj: dict) -> str | None:
    if obj.get("role") != "user":
        return None
    raw = _text_from_content(obj.get("content"))
    if not raw:
        return None
    match = _USER_QUERY_RE.search(raw)
    if match:
        text = match.group(1).strip()
        return text or None
    # 无 user_query 包裹的短纯文本也认；大段 <user_info>/rules 上下文丢掉
    if "<" in raw[:80] or len(raw) > 2000:
        return None
    return raw.strip() or None


def _assistant_text_from_blob(obj: dict) -> str | None:
    if obj.get("role") != "assistant":
        return None
    text = _text_from_content(obj.get("content")).strip()
    return text or None


def load_conversation(path: str) -> list[ConversationMessage]:
    """按 store.db 中 JSON 消息 blob 的 rowid 顺序提取 user/assistant 正文。

    path 可以是会话目录或其中的 store.db。二进制 DAG blob 跳过。
    无 store.db 时回退 prompt_history（仅用户侧，oldest→newest）。
    """
    if os.path.isdir(path):
        chat_dir = path
        store_path = os.path.join(path, "store.db")
    else:
        store_path = path
        chat_dir = os.path.dirname(path)

    if os.path.isfile(store_path):
        messages: list[ConversationMessage] = []
        try:
            uri = f"file:{os.path.abspath(store_path)}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                rows = conn.execute(
                    "SELECT rowid, data FROM blobs ORDER BY rowid"
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for _, data in rows:
            if not isinstance(data, (bytes, bytearray, memoryview)):
                continue
            raw = bytes(data)
            if not raw.startswith(b"{"):
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            user_text = _user_text_from_blob(obj)
            if user_text is not None:
                messages.append(ConversationMessage("user", user_text))
                continue
            agent_text = _assistant_text_from_blob(obj)
            if agent_text is not None:
                messages.append(ConversationMessage("assistant", agent_text))
        if messages:
            return messages

    prompts = _prompt_history(chat_dir)
    # newest-first → chronological
    return [
        ConversationMessage("user", text)
        for text in reversed(prompts)
    ]


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Cursor CLI 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
