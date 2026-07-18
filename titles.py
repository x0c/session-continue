#!/usr/bin/env python3
"""会话标题解析：缓存 / 临时兜底 / 后台批量生成。

Claude Code 自带 aiTitle 不稳定，不能作为产品展示标题的可信来源。
统一策略是：先用缓存里的生成标题；没有缓存时显示临时兜底，并提交后台生成。
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import titlegen
from models import session_key

CACHE_DIR = os.path.expanduser("~/.cache/pickup")
_LEGACY_CACHE_DIR = os.path.expanduser("~/.cache/session-continue")
CACHE_FILE = os.path.join(CACHE_DIR, "titles.json")
TITLE_CACHE_VERSION = 3


def _migrate_legacy_cache_dir() -> None:
    """项目改名 session-continue → pickup 后一次性迁移旧缓存目录，避免标题重新生成花钱。"""
    if os.path.isdir(_LEGACY_CACHE_DIR) and not os.path.exists(CACHE_DIR):
        try:
            os.rename(_LEGACY_CACHE_DIR, CACHE_DIR)
        except OSError:
            pass


_migrate_legacy_cache_dir()

# 状态列统一枚举：Claude / Codex 两个来源共用同一套标签和判定优先级。
# 优先级（高到低）：已中断 > 待回复 > 已完成 > 空（无法判断末轮角色时不展示状态）。
STATUS_ABORTED = "⚠️已中断"
STATUS_PENDING = "⏳待回复"
STATUS_DONE = "✅已完成"
STATUS_NONE = ""

# 每条摘录截断长度，控制批量 prompt 体量
_EXCERPT_LEN = 300
_TEMP_TITLE_LEN = 26

# 标题生成 prompt 的固定开头：用 `claude -p` 生成标题时会在用户本机留下一条新的
# Claude Code 会话记录（cwd 为运行 sc 时所在目录）。scan_claude.py 用这个前缀
# 识别并过滤掉这类自产生的噪音会话，避免它们污染会话列表。
PROMPT_MARKER = "你将看到一批编程助手会话的摘录"

_DOC_COMMAND_LABELS = {
    "doc-init": "文档初始化",
    "doc-update": "会话文档复盘",
    "doc-compact": "文档整理压缩",
    "doc-audit": "文档审查",
}


def _fingerprint(session: dict) -> str:
    """用内容大小做指纹；展示时间变化不应导致标题缓存失效。"""
    return f"v{TITLE_CACHE_VERSION}:{session.get('size_bytes', session['size_kb'])}"


def _cached_entry(session: dict, cache: dict) -> dict | None:
    """优先读取运行时隔离的新键，并兼容已有的纯会话 ID 缓存。"""
    return cache.get(session_key(session)) or cache.get(session["id"])


def load_cache() -> dict:
    if not os.path.isfile(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    """原子写：后台标题生成进程逐批写、TUI 每秒轮询读同一份缓存文件，
    直接覆写会被并发读到半截 JSON（load_cache 解析失败退回空字典，界面
    标题短暂回退临时兜底、转圈圈重置）。先写临时文件再 os.replace 落地，
    读取方任何时刻看到的都是完整的旧版本或新版本，不会看到半截内容。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = CACHE_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CACHE_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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


def _normalize_title(text: str | None) -> str | None:
    line = _title_line(text)
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


def _is_machine_slug(text: str | None) -> bool:
    line = _title_line(text)
    if not line:
        return False
    return bool(re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+){2,}", line))


def _is_low_value_title(text: str | None) -> bool:
    line = _title_line(text)
    if not line:
        return True
    if line in {"...", "…"}:
        return True
    if line.startswith(("{", "[")):
        return True  # 结构化 JSON/数组片段；截断或被 pretty-print 折行后未必能 fullmatch 闭合括号
    compact = re.sub(r"[\s,，。.!！?？:：;；'\"`~～…\[\]()（）{}<>《》]+", "", line).lower()
    if compact in {
        "继续",
        "继续吧",
        "你继续",
        "在吗",
        "在",
        "快点",
        "快点儿",
        "快点啊",
        "好",
        "好的",
        "ok",
        "yes",
        "no",
        "requestinterruptedbyuser",
        "continuefromwhereyouleftoff",
        "noresponserequested",
    }:
        return True
    if compact.startswith(("你测试了吗", "测试了吗", "快点继续")):
        return True
    if line.startswith((
        PROMPT_MARKER,
        "API Error:",
        "Base directory for this skill:",
        "This page isn't working",
        "This page isn’t working",
        "You've hit your session limit",
        "No response requested.",
        "你是 OpenConductor 的管理者 Agent",
        "你是 OpenConductor 的聊天意图解析器",
    )):
        return True
    return len(compact) <= 8 and compact.startswith(("继续", "快点"))


def _compact_title(text: str | None) -> str | None:
    line = _normalize_title(text)
    if not line or _is_low_value_title(line) or _is_machine_slug(line):
        return None
    line = re.sub(r"https?://\S+", "", line)
    line = re.sub(r"@?~?/[\w./-]+", "", line)
    line = re.sub(r"^\d+[.、)）-]+\s*", "", line).strip()
    line = re.sub(r"^(帮我把|帮我|请|麻烦|你帮我|你看下|看下|我现在|现在)\s*", "", line)
    line = re.sub(r"\s+", " ", line).strip(" ，。.!！?？:：;；`'\"")
    if not line:
        return None

    parts = [part.strip() for part in re.split(r"[，。!！?？;；:：\n]", line) if part.strip()]
    if parts and len(parts[0]) >= 4:
        line = parts[0]
    if len(line) > _TEMP_TITLE_LEN:
        return line[:_TEMP_TITLE_LEN] + "…"
    return line


def _temporary_title(session: dict) -> str | None:
    user_candidates = [
        _compact_title(session.get("fallback_title")),
        _compact_title(session.get("first_user_msg")),
        _compact_title(session.get("last_user_msg")),
        _compact_title(session.get("native_title")),
    ]
    user_candidates = [candidate for candidate in user_candidates if candidate]
    if user_candidates:
        return min(user_candidates, key=len)
    return _compact_title(session.get("last_agent_msg"))


def resolve_initial_title(session: dict, cache: dict) -> tuple[str, bool]:
    """返回 (标题, 是否还需要后台生成)。

    策略：
    - 缓存命中生成标题 → 直接用。
    - 缓存缺失或失效 → 显示临时兜底，并提交后台生成。
    - 原生标题只在兜底标题不可用时作为临时占位，不作为最终展示来源。
    """
    cached = _cached_entry(session, cache)
    cached_title = _normalize_title(cached.get("title") if cached else None)
    if cached and not _is_low_value_title(cached_title) and not _is_machine_slug(cached_title):
        return cached_title, cached.get("fp") != _fingerprint(session)

    temporary = _temporary_title(session)
    if temporary:
        return temporary, True

    return "(待生成标题)", True


def has_usable_cached_title(session: dict, cache: dict) -> bool:
    cached = _cached_entry(session, cache)
    cached_title = _normalize_title(cached.get("title") if cached else None)
    return bool(cached and not _is_low_value_title(cached_title) and not _is_machine_slug(cached_title))


def _build_batch_prompt(sessions: list[dict]) -> str:
    items = []
    for s in sessions:
        items.append(
            {
                "id": session_key(s),
                "preferred_title": s.get("fallback_title", "")[:_EXCERPT_LEN],
                "first_user_msg": s.get("first_user_msg", "")[:_EXCERPT_LEN],
                "last_user_msg": s.get("last_user_msg", "")[:_EXCERPT_LEN],
                "last_agent_msg": s.get("last_agent_msg", "")[:_EXCERPT_LEN],
            }
        )
    payload = json.dumps(items, ensure_ascii=False)
    return (
        f"{PROMPT_MARKER}（JSON 数组，每项含 id 和首尾消息片段）。"
        "为每条会话生成一个不超过 16 个字的标题，概括这次会话在做什么。"
        "preferred_title 是扫描器选出的最佳用户意图，优先依据它；"
        "只有它不清楚时才参考 first_user_msg、last_user_msg、last_agent_msg。"
        "标题语言规则：如果会话内容主要是中文，就用中文；如果主要是英文，也可以用中文描述，但优先用简洁的中文。"
        "只输出一个 JSON 对象，键是 id，值是标题字符串，不要输出任何其他文字。\n\n"
        f"{payload}"
    )


def generate_titles_batch(sessions: list[dict], generator: "titlegen.TitleGenerator | None", timeout: int = 90) -> dict[str, str]:
    """通过标题生成器批量生成标题,返回 {id: title}。失败时返回空字典。"""
    if not sessions or generator is None:
        return {}

    text = generator.generate(_build_batch_prompt(sessions), timeout=timeout)
    if not text:
        return {}

    text = text.strip()
    # 模型可能用 ```json 包裹,剥掉代码块标记
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v).strip() for k, v in data.items() if v}
    except json.JSONDecodeError:
        pass
    return {}


_BATCH_SIZE = 5  # 每次模型调用处理 5 条会话，控制单条提示词体量。
_MAX_PARALLEL_BATCHES = 5  # 最多同时运行 5 批，即至多并行补全 25 条标题。


def refresh_titles(sessions: list[dict], cache: dict, generator: "titlegen.TitleGenerator | None" = None) -> dict[str, str]:
    """对一批待生成的会话批量生成标题,写回缓存,返回 {会话键: title} 增量。

    内部按 _BATCH_SIZE 拆批，并以最多 _MAX_PARALLEL_BATCHES 批并行生成。
    例如 25 条待生成会话会启动 5 个并行任务，每个任务处理 5 条；超过
    25 条时，完成的任务会继续领取下一批，避免同时启动过多模型进程。
    generator 为 None 时按环境自动选择;本机没有任何可用 CLI 时返回空增量。
    """
    if not sessions:
        return {}
    if generator is None:
        generator = titlegen.resolve_generator()
    if generator is None:
        return {}

    merged: dict[str, str] = {}

    chunks = [sessions[i: i + _BATCH_SIZE] for i in range(0, len(sessions), _BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_BATCHES, len(chunks))) as pool:
        futures = {pool.submit(generate_titles_batch, chunk, generator): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            raw = future.result()
            chunk_updated = False
            for s in chunk:
                key = session_key(s)
                title = _normalize_title(raw.get(key) or raw.get(s["id"]))
                if title and not _is_low_value_title(title) and not _is_machine_slug(title):
                    merged[key] = title
                    cache[key] = {"fp": _fingerprint(s), "title": title}
                    chunk_updated = True
            # 每批完成立即落盘：后台任务并行生成时，TUI 轮询能尽早读到新标题，
            # 而不是等所有批次结束才一次性可见。缓存仍只在当前线程写入，避免竞争。
            if chunk_updated:
                save_cache(cache)

    return merged


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from scan_codex import scan_sessions as scan_codex_sessions

    sessions = scan_codex_sessions(limit=5)
    cache = load_cache()
    pending = []
    for s in sessions:
        title, needs_gen = resolve_initial_title(s, cache)
        print(f"[{s['short_id']}] 初始标题={title!r} 待生成={needs_gen}")
        if needs_gen:
            pending.append(s)

    if pending:
        print(f"\n后台生成 {len(pending)} 条...")
        result = refresh_titles(pending, cache)
        for s in pending:
            print(f"[{s['short_id']}] 生成结果={result.get(s['id'])!r}")
