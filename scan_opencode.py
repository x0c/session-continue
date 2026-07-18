#!/usr/bin/env python3
"""扫描 OpenCode 会话历史（SQLite opencode.db），输出统一会话结构。

OpenCode v1.2.0 起把历史存进单个 SQLite 数据库（session/message/part 三表，
WAL 模式），更早版本的 JSON 文件存储不做兼容——官方升级会自动迁移，遗留用户
极少；本机没有 opencode.db 时该运行时的会话列表就是空的，不报错。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from itertools import groupby

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titles
from models import ConversationMessage, format_message_time
from scan_common import live_pids_by_process_name, shorten_cwd as _shorten_cwd

DB_FILENAME = "opencode.db"

_SCAN_SQL = """
SELECT
  s.id, s.directory, s.title, s.time_created, s.time_updated,
  (SELECT m.data FROM message m WHERE m.session_id = s.id
     ORDER BY m.time_created DESC, m.id DESC LIMIT 1)              AS last_msg_data,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'user'
     ORDER BY m.time_created ASC, m.id ASC, p.id ASC LIMIT 1)      AS first_user_text,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'user'
     ORDER BY m.time_created DESC, m.id DESC, p.id DESC LIMIT 1)   AS last_user_text,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'assistant'
     ORDER BY m.time_created DESC, m.id DESC, p.id DESC LIMIT 1)   AS last_agent_text,
  (SELECT COALESCE(SUM(LENGTH(p.data)), 0) FROM part p
     WHERE p.session_id = s.id)                                     AS content_bytes
FROM session s
WHERE s.parent_id IS NULL
  AND s.time_archived IS NULL
ORDER BY s.time_updated DESC
LIMIT ?
"""

_CONVERSATION_SQL = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data, p.data AS part_data
FROM message m JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND json_extract(p.data, '$.type') = 'text'
  AND json_extract(p.data, '$.synthetic') IS NOT 1
ORDER BY m.time_created ASC, m.id ASC, p.id ASC
"""


def _db_paths() -> list[str]:
    """按 OPENCODE_DATA_DIR（可逗号分隔）→ XDG_DATA_HOME → 默认路径的次序解析 db 文件。"""
    data_dir = os.environ.get("OPENCODE_DATA_DIR", "").strip()
    if data_dir:
        dirs = [d.strip() for d in data_dir.split(",") if d.strip()]
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = xdg if xdg else os.path.expanduser("~/.local/share")
        dirs = [os.path.join(base, "opencode")]
    return [p for p in (os.path.join(d, DB_FILENAME) for d in dirs) if os.path.isfile(p)]


def _connect_ro(db_path: str) -> sqlite3.Connection | None:
    """只读打开；WAL 库在极端情况下（需要恢复且无活跃写者）可能拒绝只读打开，静默降级。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _status_tag(last_msg_data: str | None) -> str:
    """末轮状态判定，与 scan_claude.py / scan_codex.py 共用 titles.py 里的统一枚举。

    OpenCode 没有 Codex 那样显式的中断事件；finish 为 tool-calls/unknown 时
    宁可不下判断（STATUS_NONE），只有消息里带非空 error 字段才判已中断。
    """
    if not last_msg_data:
        return titles.STATUS_NONE
    try:
        msg = json.loads(last_msg_data)
    except json.JSONDecodeError:
        return titles.STATUS_NONE
    if not isinstance(msg, dict):
        return titles.STATUS_NONE
    role = msg.get("role")
    if role == "user":
        return titles.STATUS_PENDING
    if role == "assistant":
        if msg.get("error"):
            return titles.STATUS_ABORTED
        if msg.get("finish") == "stop":
            return titles.STATUS_DONE
    return titles.STATUS_NONE



def _build_session_info(row: sqlite3.Row, db_path: str) -> dict | None:
    cwd = row["directory"] or ""
    first_user = str(row["first_user_text"] or "")
    native_title = row["title"] or None
    fallback = first_user.split("\n")[0].strip()
    if len(fallback) > 60:
        fallback = fallback[:60] + "…"
    if not fallback:
        fallback = "(无消息)"
    if not native_title and fallback == "(无消息)":
        return None  # 既无原生标题也无用户正文的空会话，无展示价值

    session_id = str(row["id"])
    mtime = row["time_updated"] / 1000
    size_bytes = int(row["content_bytes"] or 0)

    return {
        "source": "opencode",
        "id": session_id,
        "short_id": session_id[:12],
        "cwd": cwd,
        "cwd_display": _shorten_cwd(cwd),
        "mtime": mtime,
        "display_time": format_message_time(mtime),
        "time_source": "db_time_updated",
        "event_time": mtime,
        "file_mtime": mtime,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "native_title": native_title,
        "fallback_title": fallback,
        "status_tag": _status_tag(row["last_msg_data"]),
        "live": False,  # scan_sessions 统一按 live_pids_by_process_name() 回填
        "pid": None,
        "first_user_msg": first_user[:300],
        "last_user_msg": str(row["last_user_text"] or "")[:300],
        "last_agent_msg": str(row["last_agent_text"] or "")[:300],
        "path": db_path,
    }


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描所有 OpenCode 数据目录下的会话，返回统一结构列表，按 mtime 降序。

    每个数据目录一条 SQL 拿 top-limit 条候选（已过滤子代理会话和已归档会话），
    多目录结果合并后再按 mtime 重排、截到 limit——实测单条 SQL（含四个预览
    子查询）耗时个位数毫秒，远在首屏 ≤1s 预算内，无需额外的早停优化。
    """
    live_by_cwd = live_pids_by_process_name("opencode")
    results: list[dict] = []
    for db_path in _db_paths():
        conn = _connect_ro(db_path)
        if conn is None:
            continue
        try:
            rows = conn.execute(_SCAN_SQL, (limit,)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        for row in rows:
            info = _build_session_info(row, db_path)
            if info is None:
                continue
            if cwd_filter and not info["cwd"].startswith(cwd_filter):
                continue
            results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]
    for info in results:  # 已按 mtime 降序，同 cwd 只把最新一条标记存活
        cwd = info.get("cwd") or ""
        pid = live_by_cwd.pop(os.path.realpath(cwd), None) if cwd else None
        if pid is not None:
            info["live"] = True
            info["pid"] = pid
    return results


def load_conversation(db_path: str, session_id: str) -> list[ConversationMessage]:
    """按时间顺序读取用户消息和助手最终答复；同一消息的多个 text part 合并为一条。"""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(_CONVERSATION_SQL, (session_id,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    messages: list[ConversationMessage] = []
    for _, group_iter in groupby(rows, key=lambda r: r["message_id"]):
        group = list(group_iter)
        try:
            msg = json.loads(group[0]["msg_data"]) or {}
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        texts = []
        for r in group:
            try:
                part = json.loads(r["part_data"]) or {}
            except json.JSONDecodeError:
                continue
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
        text = "\n\n".join(texts)
        if not text:
            continue

        created = (msg.get("time") or {}).get("created")
        timestamp = created / 1000 if isinstance(created, (int, float)) else None
        messages.append(ConversationMessage(role, text, timestamp))
    return messages


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 OpenCode 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'进行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r} "
            f"status={s['status_tag']!r}"
        )
