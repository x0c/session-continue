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
DEFAULT_LIST_FIELDS = (
    "id", "short_id", "runtime", "title", "status", "mtime", "cwd_display",
    "resumable", "resume_command",
)
# search 的精简字段集在 list 基础上额外保留命中方式、命中字段和相关性得分，方便调用方理解排序
DEFAULT_SEARCH_FIELDS = DEFAULT_LIST_FIELDS + ("matched_via", "matched_fields", "score")
DEFAULT_SHOW_FIELDS = DEFAULT_LIST_FIELDS + ("messages", "message_count_shown", "message_count_total")


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
    fields = _parse_fields(None, DEFAULT_SHOW_FIELDS if compact else None)
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
        "cwd": handoff.original_cwd,
        "cwd_exists": bool(usable_cwd(handoff.original_cwd)),
        "history_path": handoff.history_path,
        "history_reading_hint": handoff.history_reading_hint,
        "suggested_prompt": handoff.render_prompt(),
        "resume_command": resume_command,
    })


def cmd_describe(args, registry) -> dict:
    if args.target:
        spec = next((c for c in COMMANDS if c["name"] == args.target), None)
        if spec is None:
            raise ApiError(
                "not_found", f"未知命令：{args.target}", EXIT_NOT_FOUND,
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
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段"}},
        ],
        "fields": {
            "...": "与 list 命令的字段相同",
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
        ],
        "fields": {
            "...": "与 list 命令的字段相同",
            "messages": "[{role: user|assistant|system, text}]，按时间顺序的用户消息、每轮最终答复和系统事件（system 是 Monitor/task-notification 等非用户手动输入的系统注入事件，不代表用户说了这句话）",
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
            "cwd": "原会话工作目录",
            "cwd_exists": "该目录在当前机器上是否仍然存在",
            "history_path": "历史 JSONL 文件绝对路径",
            "history_reading_hint": "如何解读该运行时历史格式的提示",
            "suggested_prompt": "跨运行时接力时建议使用的首条提示词（人类或 Agent 可直接复用）",
            "resume_command": "同运行时原生恢复该会话的 shell 命令（可能为 null）",
        },
    },
    {
        "name": "describe",
        "help": "查看命令列表或某个命令的完整参数 / 输出字段说明",
        "args": [
            {"flags": ["target"], "kwargs": {"nargs": "?", "help": "命令名；省略则列出全部命令"}},
        ],
        "fields": {},
    },
]

HANDLERS = {
    "list": cmd_list,
    "search": cmd_search,
    "show": cmd_show,
    "context": cmd_context,
    "describe": cmd_describe,
}

COMMAND_NAMES = tuple(spec["name"] for spec in COMMANDS)


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(
        prog="sc",
        description="sc 的机器可读数据接口：只读，供大模型 Agent 查询本地会话。",
    )
    sub = parser.add_subparsers(dest="command")
    for spec in COMMANDS:
        sp = sub.add_parser(spec["name"], help=spec["help"])
        for arg in spec.get("args", []):
            sp.add_argument(*arg["flags"], **arg["kwargs"])
    return parser


def dispatch(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("缺少子命令，请使用 sc describe 查看可用命令")
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
