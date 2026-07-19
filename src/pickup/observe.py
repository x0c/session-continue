"""本地 TUI 可观测性：结构化事件写到 ~/.cache/pickup/events.log。

TUI 占终端时 stderr 不可见，诊断只能落盘。默认只记低基数事件；
PICKUP_DEBUG=1 / PICKUP_LOG=debug 或 init(debug=True) 才写 debug 级。
写失败必须吞掉，不能拖死抓帧/重扫线程。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

CACHE_DIR = os.path.expanduser("~/.cache/pickup")
EVENTS_LOG = os.path.join(CACHE_DIR, "events.log")
EMBED_ERROR_LOG = os.path.join(CACHE_DIR, "embed-error.log")
_MAX_LOG_BYTES = 256 * 1024

_REDACT_KEYS = frozenset({
    "text", "prompt", "messages", "message", "content", "body",
    "argv", "command", "token", "api_key", "password", "secret",
})

_lock = threading.Lock()
_debug = False
_inited = False


def reset_for_tests() -> None:
    """单测专用：清掉进程内开关状态。"""
    global _debug, _inited
    with _lock:
        _debug = False
        _inited = False


def init(*, debug: bool | None = None) -> None:
    """启动时调用一次。debug=None 时读 PICKUP_DEBUG / PICKUP_LOG=debug。"""
    global _debug, _inited
    if debug is None:
        env_debug = os.environ.get("PICKUP_DEBUG", "").strip() not in ("", "0", "false", "False")
        env_log = os.environ.get("PICKUP_LOG", "").strip().lower() == "debug"
        debug = env_debug or env_log
    with _lock:
        _debug = bool(debug)
        _inited = True


def _ensure_inited() -> None:
    if not _inited:
        init()


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _REDACT_KEYS or key.lower() in _REDACT_KEYS:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


def _truncate_if_needed(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG_BYTES:
            os.truncate(path, 0)
    except OSError:
        pass


def _write_line(path: str, payload: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _truncate_if_needed(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def event(name: str, **fields: Any) -> None:
    """默认 info 级结构化事件。"""
    _ensure_inited()
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": "info",
        "name": name,
        **_sanitize(fields),
    }
    with _lock:
        _write_line(EVENTS_LOG, payload)


def debug(name: str, **fields: Any) -> None:
    """仅 debug 开启时写入。"""
    _ensure_inited()
    with _lock:
        if not _debug:
            return
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": "debug",
            "name": name,
            **_sanitize(fields),
        }
        _write_line(EVENTS_LOG, payload)


@contextmanager
def timed(name: str, **fields: Any) -> Iterator[None]:
    """结束时写带 duration_ms 的 event。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        event(name, duration_ms=duration_ms, **fields)


def log_exception(where: str, exc: BaseException) -> None:
    """异常：events.log 一条无 traceback 的 error；embed-error.log 留完整栈。"""
    _ensure_inited()
    event(
        "error",
        where=where,
        exc_type=type(exc).__name__,
        exc_msg=str(exc)[:200],
    )
    try:
        os.makedirs(os.path.dirname(EMBED_ERROR_LOG) or ".", exist_ok=True)
        _truncate_if_needed(EMBED_ERROR_LOG)
        with open(EMBED_ERROR_LOG, "a", encoding="utf-8") as fh:
            if exc.__traceback__ is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                tb = f"{type(exc).__name__}: {exc}\n"
            fh.write(
                f"{datetime.now().isoformat(timespec='seconds')} [{where}] "
                f"{type(exc).__name__}: {exc}\n{tb}\n"
            )
    except OSError:
        pass


SCREENSHOTS_DIR_NAME = "screenshots"


def save_tui_screenshot(app) -> str:
    """把当前 Textual App 导出到 ~/.cache/pickup/screenshots/；返回 SVG 路径。

    用户主动触发=知情同意；勿把含真实对话的截图提交进仓库。
    SVG 真彩色可能被压成灰阶，配色验收仍以真机为准。
    """
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(CACHE_DIR, SCREENSHOTS_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"tui-{stamp}.svg"
    path = app.save_screenshot(filename, path=out_dir)
    event("screenshot", path=str(path), format="svg")
    return str(path)


def log_embed_error(where: str, exc: BaseException) -> None:
    """内嵌后台线程的异常记录：TUI 接管终端期间 stderr 不可见，写文件留证。

    events.log 一条结构化 error，embed-error.log 留 traceback。
    """
    log_exception(where, exc)


# 兼容旧名
_log_embed_error = log_embed_error

