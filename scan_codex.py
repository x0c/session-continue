#!/usr/bin/env python3
"""扫描 Codex 会话历史（~/.codex/sessions/），输出统一会话结构。

移植自 agentsync 的 codex-session-continue/scripts/list_sessions.py，
去掉了 CLI/表格输出，只保留 scan_sessions() 供 sc.py 消费。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titles
from models import ConversationMessage, effective_session_time, format_message_time

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


_format_display_time = format_message_time


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
    thread_source = None
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
            thread_source = payload.get("thread_source")
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
    resolved_event_time = event_time or (dt.timestamp() if dt else None)
    session_time, time_source = effective_session_time(mtime, resolved_event_time)
    size_bytes = os.path.getsize(path)
    size_kb = size_bytes / 1024
    fallback = (first_user_msg or "").split("\n")[0].strip()
    if len(fallback) > 60:
        fallback = fallback[:60] + "…"
    if not fallback:
        fallback = "(无消息)"

    return {
        "source": "codex",
        "id": uuid,
        "short_id": uuid[:8],
        "thread_source": thread_source,
        "cwd": cwd or "",
        "cwd_display": _shorten_cwd(cwd or ""),
        "mtime": session_time,
        "display_time": _format_display_time(session_time),
        "time_source": time_source,
        "event_time": resolved_event_time,
        "file_mtime": mtime,
        "size_bytes": size_bytes,
        "size_kb": round(size_kb, 1),
        "native_title": index.get(uuid),
        "fallback_title": fallback,
        "status_tag": _status_tag(last_event_type),
        "live": False,  # scan_sessions 统一按 _live_session_ids() 回填
        "pid": None,  # 同上，运行中会话的进程号
        "first_user_msg": (first_user_msg or "")[:300],
        "last_user_msg": (last_user_msg or "")[:300],
        "last_agent_msg": (last_agent_msg or "")[:300],
        "path": path,
    }


def _live_session_ids() -> dict[str, int]:
    """返回进程仍存活的 Codex 会话 UUID -> pid 映射。

    Codex 没有类似 Claude 的 pid 注册表，但活着的 codex 进程会以写模式持有
    自己的 rollout JSONL（实测 lsof 输出形如
    `codex 47372 … 45w … rollout-2026-07-04T16-03-52-<uuid>.jsonl`）。
    先用 pgrep 拿到所有 codex 进程 pid，再逐个 lsof 从其打开的文件里抽 UUID，
    顺手记下 pid，供 Agent 接口把「哪个会话在跑」精确到进程号。
    任一环节缺工具或调用失败都静默降级为空集（判活失败时全部按已结束显示）。
    """
    live_ids: dict[str, int] = {}
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", "codex"], stderr=subprocess.DEVNULL
        ).decode().split()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return live_ids

    for pid_str in pids:
        try:
            out = subprocess.check_output(
                ["lsof", "-p", pid_str], stderr=subprocess.DEVNULL
            ).decode(errors="replace")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        for line in out.splitlines():
            if "rollout-" not in line:
                continue
            uuid = _extract_uuid_from_filename(line)
            if uuid:
                live_ids[uuid] = pid
    return live_ids


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[dict]:
    """扫描 Codex 会话，返回统一结构列表，按 mtime 降序。

    历史会话可能有上千个，但调用方只要最近 limit 条。_build_session_info 要
    读文件头尾解析 JSONL 较慢，所以先用廉价的 os.stat 按真实文件 mtime（而非
    文件名里的创建时间——同一会话被续接会更新 mtime 但不改文件名）排好序，
    凑够 limit 条有效结果就提前停止，不必解析全部历史文件。

    首屏必须 ≤1s（见 AGENTS.md 验证要求）：cwd 判活按 cwd 记忆化，避免大量
    会话共享同一个 cwd 时重复 os.path.isdir——这个调用在同步/网络目录上很
    慢，实测是首屏卡顿主因之一（结果与逐次调用字节级一致）。
    """
    index = _load_index()
    all_files = _find_all_session_files()
    live_ids = _live_session_ids()

    candidates: list[tuple[float, str]] = []
    for path in all_files:
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    for _, path in candidates:
        try:
            info = _build_session_info(path, index)
        except OSError:
            continue
        if info is None:
            continue
        if info["thread_source"] == "subagent":
            continue  # Codex 自身多智能体拆出的子代理线程，不是用户发起的顶层会话，会与父会话共享同一段历史开头造成列表重复
        if not info["first_user_msg"] or info["fallback_title"] == "(无消息)":
            continue  # 无用户消息的空会话
        if info["cwd"] and not cached_isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        info["live"] = info["id"] in live_ids
        info["pid"] = live_ids.get(info["id"])
        results.append(info)
        if len(results) >= limit:
            break

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真实用户消息和 Codex 的助手消息（含任务执行中的过程叙述 commentary 和最终答复 final_answer）。"""
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

                # payload 里的字段即使写了 key，值也可能是 JSON null（如任务无输出就结束的
                # task_complete）；`.get(key, "")` 只在 key 缺失时才用默认值，key 存在但值为
                # null 时会拿到 None，`str(None)` 变成字面量 "None" 混进正文，必须用 `or ""` 兜底。
                payload_type = payload.get("type")
                if payload_type == "user_message":
                    text = str(payload.get("message") or "").strip()
                    if text:
                        messages.append(ConversationMessage("user", text, _entry_time(entry)))
                elif payload_type == "agent_message" and payload.get("phase") in (None, "final_answer", "commentary"):
                    text = str(payload.get("message") or "").strip()
                    if text and (not messages or messages[-1].role != "assistant" or messages[-1].text != text):
                        messages.append(ConversationMessage("assistant", text, _entry_time(entry)))
                elif payload_type == "task_complete":
                    text = str(payload.get("last_agent_message") or "").strip()
                    if text and (not messages or messages[-1].role != "assistant" or messages[-1].text != text):
                        messages.append(ConversationMessage("assistant", text, _entry_time(entry)))
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
            f"{s['size_kb']:>7}KB {'进行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
