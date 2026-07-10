#!/usr/bin/env python3
"""sc 的机器可读数据接口：面向大模型 Agent 的只读命令集合。

sc 只负责把本地会话数据结构化地交出来（列表 / 搜索 / 详情 / 接续上下文），
不负责决定"拿到数据之后做什么"——不新增自动拉起、后台执行等副作用命令。
所有命令输出统一 JSON envelope：{ok, data, error, meta}。

退出码：0 成功、1 一般失败、2 用法错误、3 会话不存在、5 会话标识有歧义。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import titles
from models import session_key
from runtime import LaunchError, default_registry
from runtime.base import usable_cwd

AGENT_API_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 5

STATUS_LABELS = {
    titles.STATUS_DONE: "done",
    titles.STATUS_PENDING: "pending",
    titles.STATUS_ABORTED: "aborted",
    titles.STATUS_NONE: "unknown",
}

_RESOLVE_SCAN_LIMIT = 200  # show/context 按标识定位会话时的扫描深度，独立于 list/search 的展示条数

# --compact 模式下 list 的精简默认字段集（省 token）；需要更多字段用 --fields 显式指定
# live/last_user/last_agent 默认就带上：管家 Agent 一眼看懂"这条会话在跑没跑、最近聊了什么"，
# 不必为此再多一次 show 往返。pid 体积小但多数为 null，不进精简集，需要时用 --fields 或 --live 取。
DEFAULT_LIST_FIELDS = (
    "id", "short_id", "runtime", "title", "status", "live", "mtime", "cwd_display",
    "resumable", "resume_command", "last_user", "last_agent",
)
# search 的精简字段集在 list 基础上额外保留命中方式、命中字段和相关性得分，方便调用方理解排序
DEFAULT_SEARCH_FIELDS = DEFAULT_LIST_FIELDS + ("matched_via", "matched_fields", "score")
DEFAULT_SHOW_FIELDS = DEFAULT_LIST_FIELDS + ("messages", "message_count_shown", "message_count_total")

_SUMMARY_TRIM_LEN = 120  # last_user/last_agent 摘要的硬截断长度


def _trim(text: str | None, limit: int = _SUMMARY_TRIM_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class ApiError(Exception):
    """携带退出码和结构化提示的命令级错误，由 dispatch 统一转成 JSON envelope。"""

    def __init__(self, code, message, exit_code=EXIT_ERROR, hint=None, next_commands=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.next_commands = next_commands or []


class JSONArgumentParser(argparse.ArgumentParser):
    """参数错误时输出 JSON envelope + 退出码 2，而不是纯文本 usage 信息。"""

    def error(self, message):
        _print_envelope({
            "ok": False,
            "data": None,
            "error": {
                "code": "usage_error",
                "message": message,
                "hint": "运行 sc describe 或 sc describe <command> 查看用法",
                "next_commands": ["sc describe"],
            },
            "meta": {"version": AGENT_API_VERSION},
        })
        raise SystemExit(EXIT_USAGE)


def _print_envelope(payload: dict, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None, "meta": {"version": AGENT_API_VERSION}}


def _err(exc: ApiError) -> dict:
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "next_commands": exc.next_commands,
        },
        "meta": {"version": AGENT_API_VERSION},
    }


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


def _resume_command(runtime, session: dict) -> str | None:
    """生成同运行时原生恢复命令；失败时只标记不可恢复，不影响列表输出。"""
    try:
        plan = runtime.build_resume_plan(session)
    except Exception:
        return None
    return _format_resume_command(plan.argv)


def _apply_fields(payload: dict, fields: list[str] | None) -> dict:
    if not fields:
        return payload
    return {k: v for k, v in payload.items() if k in fields}


def session_payload(session: dict, cache: dict, runtime=None, fields: list[str] | None = None) -> dict:
    """把内部会话结构裁剪为对 Agent 友好的输出：语义化标题、英文状态枚举。"""
    title, _ = titles.resolve_initial_title(session, cache)
    status_tag = session.get("status_tag") or ""
    resume_command = _resume_command(runtime, session) if runtime is not None else None
    payload = {
        "runtime": session.get("source"),
        "id": session.get("id"),
        "short_id": session.get("short_id"),
        "title": title,
        "cwd": session.get("cwd") or "",
        "cwd_display": session.get("cwd_display") or "",
        "time": session.get("display_time") or "",
        "mtime": session.get("mtime"),
        "size_kb": round(session.get("size_kb") or 0, 1),
        "status": STATUS_LABELS.get(status_tag, "unknown"),
        "status_tag": status_tag,
        "history_path": session.get("path") or "",
        "resumable": bool(resume_command),
        "resume_command": resume_command,
        "live": bool(session.get("live")),
        "pid": session.get("pid"),
        "last_user": _trim(session.get("last_user_msg")),
        "last_agent": _trim(session.get("last_agent_msg")),
    }
    return _apply_fields(payload, fields)


def _match_sessions(sessions: list[dict], ident: str) -> list[dict]:
    return [
        s for s in sessions
        if s.get("id") == ident or str(s.get("id") or "").startswith(ident) or s.get("short_id") == ident
    ]


def resolve_ref(registry, ref: str, limit: int) -> dict:
    """把用户提供的会话标识（完整 ID / 前缀 / runtime:id）解析为唯一会话。"""
    if not ref:
        raise ApiError("usage_error", "缺少会话标识", EXIT_USAGE)

    if ":" in ref:
        runtime_id, _, ident = ref.partition(":")
        try:
            runtime = registry.get(runtime_id)
        except LaunchError:
            raise ApiError("not_found", f"未注册的运行时：{runtime_id}", EXIT_NOT_FOUND)
        matches = _match_sessions(runtime.scan_sessions(limit), ident)
    else:
        matches = []
        for runtime in registry:
            matches.extend(_match_sessions(runtime.scan_sessions(limit), ref))

    if not matches:
        raise ApiError(
            "not_found", f"未找到匹配会话：{ref}", EXIT_NOT_FOUND,
            hint="确认会话 ID 或前缀是否正确，可先用 sc search 或 sc list 查看",
            next_commands=[f"sc search {ref}", "sc list"],
        )

    exact = [s for s in matches if s.get("id") == ref or session_key(s) == ref]
    if len(exact) == 1:
        return exact[0]
    if len(matches) > 1:
        candidates = [session_key(s) for s in matches[:10]]
        raise ApiError(
            "ambiguous", f"会话标识存在多个候选：{ref}", EXIT_AMBIGUOUS,
            hint="使用更长的前缀或完整 runtime:id",
            next_commands=[f"sc show {c}" for c in candidates],
        )
    return matches[0]


def _find_snippet(messages, keywords: list[str]) -> str | None:
    for message in messages:
        low = message.text.lower()
        for kw in keywords:
            idx = low.find(kw)
            if idx != -1:
                start = max(0, idx - 40)
                end = min(len(message.text), idx + len(kw) + 80)
                return message.text[start:end].strip()
    return None


def _parse_fields(raw: str | None, default: tuple[str, ...] | None = None) -> list[str] | None:
    if raw:
        return [f.strip() for f in raw.split(",") if f.strip()]
    if default:
        return list(default)
    return None


def _apply_top(items: list, top: int | None) -> list:
    if top is None:
        return items
    return items[:max(0, top)]


def _score_quick_match(session: dict, title: str, keywords: list[str]) -> tuple[int, list[str]]:
    sources = [
        ("title", title, 100),
        ("fallback_title", session.get("fallback_title"), 80),
        ("first_user_msg", session.get("first_user_msg"), 60),
        ("last_user_msg", session.get("last_user_msg"), 60),
        ("last_agent_msg", session.get("last_agent_msg"), 35),
        ("cwd", session.get("cwd"), 20),
        ("cwd_display", session.get("cwd_display"), 20),
    ]
    score = 0
    matched: list[str] = []
    for name, value, weight in sources:
        text = str(value or "").lower()
        if not text:
            continue
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            score += weight * hits
            matched.append(name)
    return score, matched


def cmd_list(args, registry) -> dict:
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_LIST_FIELDS if compact else None)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    cache = titles.load_cache()

    candidates = []
    for runtime in runtimes:
        for session in runtime.scan_sessions(args.limit):
            if args.status and STATUS_LABELS.get(session.get("status_tag") or "", "unknown") != args.status:
                continue
            if args.cwd and args.cwd.lower() not in str(session.get("cwd") or "").lower():
                continue
            # 用 `is True` 而不是 truthy：AgentApiTests 里的 args 常是裸 mock.Mock()，
            # 没显式设置的属性会自动生成一个真值 Mock（不是抛异常/落回默认值），
            # truthy 判断会让老测试静默被过滤成"只剩 live 会话"。
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            candidates.append((runtime, session))

    candidates.sort(key=lambda item: item[1].get("mtime") or 0, reverse=True)
    candidates = _apply_top(candidates, top)
    sessions = [session_payload(session, cache, runtime, fields) for runtime, session in candidates]

    return _ok({
        "count": len(sessions),
        "scan_limit": args.limit,
        "top": top,
        "sessions": sessions,
    })


def cmd_search(args, registry) -> dict:
    keywords = [k.lower() for k in args.keywords]
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SEARCH_FIELDS if compact else None)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    cache = titles.load_cache()

    results = []
    for runtime in runtimes:
        for session in runtime.scan_sessions(args.limit):
            # 用 `is True` 而不是 truthy：AgentApiTests 里的 args 常是裸 mock.Mock()，
            # 没显式设置的属性会自动生成一个真值 Mock（不是抛异常/落回默认值），
            # truthy 判断会让老测试静默被过滤成"只剩 live 会话"。
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            title, _ = titles.resolve_initial_title(session, cache)
            quick_parts = [
                title,
                session.get("fallback_title"),
                session.get("first_user_msg"),
                session.get("last_user_msg"),
                session.get("last_agent_msg"),
                session.get("cwd"),
                session.get("cwd_display"),
            ]
            haystack = " ".join(filter(None, quick_parts)).lower()

            if all(kw in haystack for kw in keywords):
                score, matched_fields = _score_quick_match(session, title, keywords)
                results.append((score, "quick", matched_fields, runtime, session, None))
            elif args.deep:
                messages = runtime.load_conversation(session)
                full_text = "\n".join(m.text for m in messages).lower()
                if all(kw in full_text for kw in keywords):
                    score, matched_fields = _score_quick_match(session, title, keywords)
                    matched_fields = matched_fields + ["conversation"]
                    score += 10
                    snippet = _find_snippet(messages, keywords)
                    results.append((score, "deep", matched_fields, runtime, session, snippet))

    results.sort(key=lambda item: (item[0], item[4].get("mtime") or 0), reverse=True)
    results = _apply_top(results, top)
    sessions = []
    for score, matched_via, matched_fields, runtime, session, snippet in results:
        payload = session_payload(session, cache, runtime)
        payload["score"] = score
        payload["matched_via"] = matched_via
        payload["matched_fields"] = matched_fields
        if snippet:
            payload["snippet"] = snippet
        sessions.append(_apply_fields(payload, fields))

    return _ok({
        "query": args.keywords,
        "deep": args.deep,
        "count": len(sessions),
        "scan_limit": args.limit,
        "top": top,
        "sessions": sessions,
    })


def cmd_show(args, registry) -> dict:
    session = resolve_ref(registry, args.session, args.limit)
    cache = titles.load_cache()
    runtime = registry.get(str(session.get("source") or ""))
    compact = getattr(args, "compact", False)
    out = getattr(args, "out", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SHOW_FIELDS if compact else None)
    payload = session_payload(session, cache, runtime)
    messages = runtime.load_conversation(session)
    total_messages = len(messages)
    if not args.full:
        n = args.messages if args.messages else 20
        messages = messages[-n:]

    payload["messages"] = [{"role": m.role, "text": m.text} for m in messages]
    payload["message_count_shown"] = len(payload["messages"])
    payload["message_count_total"] = total_messages

    if out:
        envelope = _ok(payload)
        output_path = os.path.abspath(out)
        parent = os.path.dirname(output_path) or "."
        if not os.path.isdir(parent):
            raise ApiError("usage_error", f"输出目录不存在：{parent}", EXIT_USAGE)
        if os.path.isdir(output_path):
            raise ApiError("usage_error", f"输出路径是目录：{output_path}", EXIT_USAGE)
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(envelope, fp, ensure_ascii=False, separators=(",", ":") if compact else None,
                      indent=None if compact else 2)
            fp.write("\n")
        summary = session_payload(session, cache, runtime, DEFAULT_LIST_FIELDS if compact else None)
        summary.update({
            "output_path": output_path,
            "output_bytes": os.path.getsize(output_path),
            "message_count_written": len(payload["messages"]),
            "message_count_total": total_messages,
            "messages_omitted": True,
        })
        return _ok(summary)

    return _ok(_apply_fields(payload, fields))


def cmd_context(args, registry) -> dict:
    session = resolve_ref(registry, args.session, args.limit)
    cache = titles.load_cache()
    title, _ = titles.resolve_initial_title(session, cache)

    runtime = registry.get(str(session.get("source") or ""))
    try:
        handoff = runtime.export_handoff(session, title)
    except LaunchError as exc:
        raise ApiError("history_unavailable", str(exc), EXIT_ERROR)

    try:
        resume_plan = runtime.build_resume_plan(session)
        resume_command = _format_resume_command(resume_plan.argv)
    except Exception:
        resume_command = None

    return _ok({
        "runtime": handoff.source_runtime_id,
        "runtime_name": handoff.source_runtime_name,
        "id": session.get("id"),
        "title": handoff.title,
        "status": STATUS_LABELS.get(session.get("status_tag") or "", "unknown"),
        "live": bool(session.get("live")),
        "pid": session.get("pid"),
        "cwd": handoff.original_cwd,
        "cwd_exists": bool(usable_cwd(handoff.original_cwd)),
        "history_path": handoff.history_path,
        "history_reading_hint": handoff.history_reading_hint,
        "suggested_prompt": handoff.render_prompt(),
        "resume_command": resume_command,
    })


def cmd_plan_continue(args, registry) -> dict:
    """生成外部执行器可用的续接计划；本函数只构造数据，绝不启动进程。"""
    instruction = args.instruction
    if not instruction or not instruction.strip():
        raise ApiError("usage_error", "续接指令不能为空", EXIT_USAGE)

    session = resolve_ref(registry, args.session, args.limit)
    runtime = registry.get(str(session.get("source") or ""))
    try:
        plan = runtime.build_continue_plan(session, instruction)
    except LaunchError as exc:
        raise ApiError(
            "not_resumable", f"该会话无法生成续接计划：{exc}", EXIT_ERROR,
            hint="确认会话所属运行时支持带新指令的原生续接",
            next_commands=[f"sc context {session_key(session)}"],
        ) from exc
    except Exception as exc:
        raise ApiError(
            "not_resumable", f"该会话无法生成续接计划：{exc}", EXIT_ERROR,
            hint="确认会话所属运行时支持带新指令的原生续接",
            next_commands=[f"sc context {session_key(session)}"],
        ) from exc

    return _ok({
        "session_ref": session_key(session),
        "runtime": runtime.id,
        "id": session.get("id"),
        "cwd": session.get("cwd") or "",
        "capabilities": {
            "resume": True,
            "continue_with_instruction": True,
            "execution": "external_only",
        },
        "launch": {
            "argv": list(plan.argv),
            "cwd": plan.cwd,
        },
    })


def cmd_describe(args, registry) -> dict:
    if args.target:
        target = args.target if isinstance(args.target, str) else " ".join(args.target)
        spec = next((c for c in COMMANDS if c["name"] == target), None)
        if spec is None:
            raise ApiError(
                "not_found", f"未知命令：{target}", EXIT_NOT_FOUND,
                hint="运行 sc describe 查看全部命令",
            )
        return _ok(_describe_command(spec, full=True))
    return _ok({"commands": [_describe_command(spec, full=False) for spec in COMMANDS]})


def _describe_command(spec: dict, full: bool) -> dict:
    entry = {
        "name": spec["name"],
        "help": spec["help"],
        "args": [
            {"flags": arg["flags"], **{k: v for k, v in arg["kwargs"].items() if k in ("help", "default", "choices", "required", "nargs")}}
            for arg in spec.get("args", [])
        ],
    }
    if full:
        entry["fields"] = spec.get("fields", {})
    return entry


COMMANDS = [
    {
        "name": "list",
        "help": "结构化列出已注册运行时的会话",
        "args": [
            {"flags": ["--runtime"], "kwargs": {"help": "只看指定运行时（claude / codex）"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50, "help": "每个运行时最多扫描多少条历史（扫描深度）"}},
            {"flags": ["--top"], "kwargs": {"type": int, "help": "最多返回多少条结果；不影响扫描深度"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--status"], "kwargs": {"choices": ["done", "pending", "aborted", "unknown"], "help": "按状态过滤"}},
            {"flags": ["--cwd"], "kwargs": {"help": "按工作目录子串过滤（大小写不敏感）"}},
            {"flags": ["--live"], "kwargs": {"action": "store_true", "help": "只返回进程仍在运行的会话"}},
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段"}},
        ],
        "fields": {
            "runtime": "运行时标识（claude / codex）",
            "id": "会话完整 ID",
            "short_id": "会话短 ID（前 8 位）",
            "title": "会话标题（缓存的生成标题或本地兜底标题，不触发新生成）",
            "cwd": "原会话工作目录绝对路径",
            "cwd_display": "工作目录的展示形式（可能是缩短路径）",
            "time": "最后更新时间（人类可读）",
            "mtime": "最后更新时间（Unix 时间戳）",
            "size_kb": "历史文件大小（KB）",
            "status": "英文状态枚举：done / pending / aborted / unknown",
            "status_tag": "中文状态标签（含图标），供人类展示用",
            "live": "进程是否真实运行中（true/false），不是文件时间推断",
            "pid": "运行中会话的进程号；非运行中为 null，可用于定位/发信号给该进程",
            "last_user": "最后一条真人消息，硬截断精简，一眼看懂最近在聊什么",
            "last_agent": "助手最后一轮回复片段，硬截断精简",
            "history_path": "历史 JSONL 文件路径",
            "resumable": "是否可生成同运行时原生恢复命令",
            "resume_command": "同运行时原生恢复该会话的 shell 命令（可能为 null）",
        },
    },
    {
        "name": "search",
        "help": "按关键词搜会话；默认搜标题/首尾消息/目录，--deep 时额外全文搜索对话内容",
        "args": [
            {"flags": ["keywords"], "kwargs": {"nargs": "+", "help": "关键词（多个关键词为 AND 关系）"}},
            {"flags": ["--deep"], "kwargs": {"action": "store_true", "help": "对未命中的会话额外读取完整对话内容再搜一遍（较慢）"}},
            {"flags": ["--runtime"], "kwargs": {"help": "只搜指定运行时"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50, "help": "每个运行时最多扫描多少条参与搜索"}},
            {"flags": ["--top"], "kwargs": {"type": int, "help": "最多返回多少条结果；不影响扫描深度"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--live"], "kwargs": {"action": "store_true", "help": "只返回进程仍在运行的会话"}},
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段"}},
        ],
        "fields": {
            "...": "与 list 命令的字段相同（含 live / pid / last_user / last_agent）",
            "score": "相关性分数；分数越高排序越靠前，同分按 mtime 倒序",
            "matched_via": "quick（元数据命中）或 deep（全文命中），兼容旧调用方",
            "matched_fields": "命中的字段列表，如 title / first_user_msg / conversation",
            "snippet": "deep 命中时的上下文片段",
        },
    },
    {
        "name": "show",
        "help": "查看单个会话的详情和对话内容",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--messages"], "kwargs": {"type": int, "help": "只显示最后 N 条消息（默认 20）"}},
            {"flags": ["--full"], "kwargs": {"action": "store_true", "help": "显示完整对话，忽略 --messages"}},
            {"flags": ["--out"], "kwargs": {"help": "把 show 结果写入指定 JSON 文件，stdout 只返回文件引用摘要"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段（覆盖 --compact 的默认字段集）"}},
        ],
        "fields": {
            "...": "与 list 命令的字段相同",
            "messages": "[{role: user|assistant, text}]，按时间顺序的用户消息和每轮最终答复；Monitor/task-notification 等系统注入事件已过滤，不出现在这里",
            "message_count_shown": "本次实际返回的消息条数",
            "message_count_total": "该会话可提取的消息总数",
            "output_path": "--out 模式下写入的 JSON 文件绝对路径",
        },
    },
    {
        "name": "context",
        "help": "生成接续该会话所需的完整上下文数据包（不执行任何操作）",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
        ],
        "fields": {
            "runtime": "运行时标识",
            "runtime_name": "运行时展示名",
            "id": "会话 ID",
            "title": "会话标题",
            "status": "英文状态枚举",
            "live": "进程是否真实运行中；为 true 时优先用 pid 定位现有进程，不要用 resume_command 再起一个新进程",
            "pid": "运行中会话的进程号；非运行中为 null",
            "cwd": "原会话工作目录",
            "cwd_exists": "该目录在当前机器上是否仍然存在",
            "history_path": "历史 JSONL 文件绝对路径",
            "history_reading_hint": "如何解读该运行时历史格式的提示",
            "suggested_prompt": "跨运行时接力时建议使用的首条提示词（人类或 Agent 可直接复用）；内含会话状态和从原会话自动提取的对话摘录，原始历史文件仍是权威来源",
            "resume_command": "同运行时原生恢复该会话的 shell 命令（可能为 null）",
        },
    },
    {
        "name": "plan continue",
        "help": "生成携带新指令的非交互式原生续接计划（只返回数据，不执行）",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--instruction"], "kwargs": {"required": True, "help": "续接时发送给原会话的新指令"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
        ],
        "fields": {
            "session_ref": "带运行时的唯一会话标识 runtime:id",
            "runtime": "运行时标识",
            "id": "原会话完整 ID",
            "cwd": "原会话工作目录；可能已不存在",
            "capabilities": "该计划的能力与边界；execution 为 external_only，sc 不会执行计划",
            "launch.argv": "不经 shell 解释的启动参数数组；新指令是其中独立的一项",
            "launch.cwd": "外部执行器启动进程时应使用的工作目录；目录不可用时为 null",
        },
    },
    {
        "name": "describe",
        "help": "查看命令列表或某个命令的完整参数 / 输出字段说明",
        "args": [
            {"flags": ["target"], "kwargs": {"nargs": "*", "help": "命令名；省略则列出全部命令，可使用 plan continue"}},
        ],
        "fields": {},
    },
]

HANDLERS = {
    "list": cmd_list,
    "search": cmd_search,
    "show": cmd_show,
    "context": cmd_context,
    "plan continue": cmd_plan_continue,
    "describe": cmd_describe,
}

COMMAND_NAMES = tuple(spec["name"] for spec in COMMANDS)
COMMAND_ROOT_NAMES = tuple(dict.fromkeys(spec["name"].split()[0] for spec in COMMANDS))


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(
        prog="sc",
        description="sc 的机器可读数据接口：只读，供大模型 Agent 查询本地会话。",
    )
    sub = parser.add_subparsers(dest="command")
    parents = {}
    for spec in COMMANDS:
        parts = spec["name"].split()
        current_sub = sub
        path = []
        for part in parts:
            path.append(part)
            key = tuple(path)
            sp = parents.get(key)
            if sp is None:
                sp = current_sub.add_parser(part, help=spec["help"] if len(path) == len(parts) else None)
                parents[key] = sp
                if len(path) < len(parts):
                    sp.set_defaults(command=" ".join(path))
                    current_sub = sp.add_subparsers(dest=f"command_part_{len(path)}")
                else:
                    sp.set_defaults(command=spec["name"])
            elif len(path) < len(parts):
                current_sub = sp.add_subparsers(dest=f"command_part_{len(path)}")
            else:
                sp.set_defaults(command=spec["name"])
        for arg in spec.get("args", []):
            sp.add_argument(*arg["flags"], **arg["kwargs"])
    return parser


def dispatch(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("缺少子命令，请使用 sc describe 查看可用命令")
        return EXIT_USAGE  # pragma: no cover — parser.error 内部已 sys.exit

    if args.command not in HANDLERS:
        parser.error(f"子命令不完整，请使用 sc describe {args.command} 查看用法")
        return EXIT_USAGE  # pragma: no cover — parser.error 内部已 sys.exit

    registry = default_registry()
    handler = HANDLERS[args.command]
    try:
        result = handler(args, registry)
    except ApiError as exc:
        _print_envelope(_err(exc), compact=getattr(args, "compact", False))
        return exc.exit_code

    _print_envelope(result, compact=getattr(args, "compact", False))
    return EXIT_OK


def main() -> None:
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
