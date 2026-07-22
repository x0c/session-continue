"""会话保活：把启动计划包进专用 tmux 后端，SSH 断开后进程继续跑。

运行时无关的启动包装层，地位类似 titles.py——不属于任何 runtime 适配器，
`runtime/registry.py` 只负责生成 `LaunchPlan`，本模块负责在执行前后包一层
tmux。使用独立 socket（`-L pickup-keepalive`）和专属配置，与用户自己的 tmux
会话/配置完全隔离，不互相污染。

匹配保活会话到已扫描出的 `SessionInfo` 时，不能只靠 tmux 会话名：
`claude --resume` 之类的原生恢复可能在内部 fork/重新注册进程，pane 里的顶层
pid 未必等于运行时自己记录的"活跃 pid"（如 `~/.claude/sessions/{pid}.json`）。
因此用一次 `ps -eo pid,ppid` 建出整机父子关系表，逐个候选 pid 向上追祖先链，
只要能追到某个 tmux pane 的顶层 pid，就判定命中——对是否发生过 fork 免疫。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

from pickup import titles
from pickup.models import LaunchPlan

SOCKET_NAME = "pickup-keepalive"
SESSION_PREFIX = "pickup-"
LEGACY_SESSION_PREFIX = "sc-"  # 项目改名 sessionContinue → pickup 前的旧会话名前缀
_DEFAULT_IDLE_HOURS = 24.0
_SUBPROCESS_TIMEOUT = 1.5
_MAX_ANCESTOR_DEPTH = 20

_BASE_ARGV = ("tmux", "-L", SOCKET_NAME)

# tmux -f 配置内容内联在代码里，而不是仓库里独立的 .conf 文件：安装产物只包含
# 明确纳入包的数据，独立配置不能依赖源码目录相对路径（曾用独立配置实测过，安装后
# 文件完全缺失，wrap_plan 在真实安装环境里会直接报 `-f` 文件不存在）。改动只改这个
# 常量即可，_ensure_config_file() 会在下次调用时自动把新内容重新落盘覆盖旧文件。
_TMUX_CONFIG = """\
# pickup 保活会话专用 tmux 配置：只在 `tmux -L pickup-keepalive` 这个独立 socket 上生效，
# 不读取、不影响用户自己的 ~/.tmux.conf。目标是让接入的会话看起来和原生终端
# 一样，感觉不到自己在 tmux 里。

set -g status off
set -g mouse on
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -g window-size latest
setw -g aggressive-resize on
set -sg escape-time 0
set -g history-limit 10000

# 无前缀直接脱离（保留标准 prefix+d 作为备用）：Ctrl-\\ 在 tmux 接管终端时不会
# 触发本地 SIGQUIT，可以放心用作"离开但保持后台运行"的快捷键。
bind-key -n C-\\\\ detach-client
"""


def _ensure_config_file() -> str:
    """把内联的 tmux 配置落盘到本地缓存目录（`~/.cache/pickup`），返回文件路径；内容有变化才重写。"""
    os.makedirs(titles.CACHE_DIR, exist_ok=True)
    path = os.path.join(titles.CACHE_DIR, "keepalive.tmux.conf")
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != _TMUX_CONFIG:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(_TMUX_CONFIG)
        except OSError:
            pass
    return path


def _env_disabled(*names: str) -> bool:
    """任一环境变量被置 0 即视为禁用；新名 PICKUP_* 与旧名 SC_* 都认。"""
    return any((os.environ.get(name) or "").strip() == "0" for name in names)


def enabled(disabled_flag: bool = False) -> bool:
    """保活默认开启；命令行开关、环境变量或已身处 tmux/screen 中时关闭。"""
    if disabled_flag:
        return False
    if _env_disabled("PICKUP_KEEPALIVE", "SC_KEEPALIVE"):
        return False
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return False
    if shutil.which("tmux") is None:
        return False
    return True


def _session_name(runtime_id: str, ident: str) -> str:
    return f"{SESSION_PREFIX}{runtime_id}-{ident[:8]}"


def new_session_ident() -> str:
    """空白新会话或跨运行时接力目标在launch前还没有历史会话 id，生成一个临时标识用于命名。"""
    return uuid.uuid4().hex[:8]


def wrap_plan(plan: LaunchPlan, runtime_id: str, ident: str) -> LaunchPlan:
    """把原始启动计划包进 tmux `new-session -A`：会话不存在则创建，已存在则直接接入。"""
    name = _session_name(runtime_id, ident)
    argv = [*_BASE_ARGV, "-f", _ensure_config_file(), "new-session", "-A", "-s", name]
    if plan.cwd:
        argv += ["-c", plan.cwd]
    argv += [
        "-e", f"PICKUP_RUNTIME={runtime_id}",
        "-e", f"PICKUP_SESSION_ID={ident}",
        # 旧变量名继续注入，兼容改名前外部可能的读取方
        "-e", f"SC_RUNTIME={runtime_id}",
        "-e", f"SC_SESSION_ID={ident}",
        "--",
        *plan.argv,
    ]
    return LaunchPlan(argv=tuple(argv), cwd=None)


def attach_plan(session: dict) -> LaunchPlan | None:
    """会话已在保活中时，返回直接接回现场的启动计划；否则返回 None。"""
    name = session.get("keepalive_name")
    if not name:
        return None
    return LaunchPlan(argv=(*_BASE_ARGV, "attach-session", "-t", name), cwd=None)


def _list_tmux_sessions(fields: str) -> list[list[str]]:
    """列出保活 socket 上的所有会话；socket 尚不存在（还没人保活过）时静默返回空列表。"""
    if shutil.which("tmux") is None:
        return []
    try:
        out = subprocess.check_output(
            [*_BASE_ARGV, "list-sessions", "-F", fields],
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if not parts or not parts[0].startswith((SESSION_PREFIX, LEGACY_SESSION_PREFIX)):
            continue
        rows.append(parts)
    return rows


def _build_ppid_map() -> dict[int, int]:
    """一次 `ps -eo pid,ppid` 拿到整机父子关系，供祖先链匹配复用；跨 macOS/Linux 通用。"""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid"], stderr=subprocess.DEVNULL, timeout=_SUBPROCESS_TIMEOUT
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    mapping: dict[int, int] = {}
    for line in out.splitlines()[1:]:  # 跳过表头
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            mapping[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return mapping


def _is_descendant(pid: int, ancestor_pid: int, ppid_map: dict[int, int]) -> bool:
    current = pid
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if current == ancestor_pid:
            return True
        parent = ppid_map.get(current)
        if parent is None or parent <= 1:
            return False
        current = parent
    return False


def annotate(sessions) -> None:
    """给命中保活的会话就地加上 `keepalive_name` 字段；不生成新列表，不改变顺序。"""
    candidates = {s.get("pid"): s for s in sessions if s.get("pid")}
    if not candidates:
        return  # 没有任何会话带存活 pid，不值得为此打一次 tmux/ps 子进程

    tmux_sessions = _list_tmux_sessions("#{session_name}|#{pane_pid}")
    if not tmux_sessions:
        return

    ppid_map = _build_ppid_map()
    for row in tmux_sessions:
        if len(row) < 2:
            continue
        name, pane_pid_text = row[0], row[1]
        try:
            pane_pid = int(pane_pid_text)
        except ValueError:
            continue
        for pid, session in candidates.items():
            if _is_descendant(pid, pane_pid, ppid_map):
                session["keepalive_name"] = name
                break


def _idle_threshold_hours() -> float:
    raw = os.environ.get("PICKUP_KEEPALIVE_IDLE_HOURS") or os.environ.get("SC_KEEPALIVE_IDLE_HOURS")
    if raw is None:
        return _DEFAULT_IDLE_HOURS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_IDLE_HOURS


def kill(name: str) -> bool:
    """手动/自动回收指定保活会话；不存在或 tmux 不可用时静默失败。"""
    if shutil.which("tmux") is None:
        return False
    try:
        subprocess.run(
            [*_BASE_ARGV, "kill-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT, check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def reap_idle(now: float | None = None) -> list[str]:
    """关闭空闲超过阈值（默认 24 小时，`PICKUP_KEEPALIVE_IDLE_HOURS=0` 禁用）的保活会话。

    会话历史仍在各自运行时的磁盘记录里，关闭的只是 tmux 后台进程，不丢数据。
    """
    threshold_hours = _idle_threshold_hours()
    if threshold_hours <= 0:
        return []
    rows = _list_tmux_sessions("#{session_name}|#{session_activity}")
    if not rows:
        return []
    if now is None:
        now = time.time()
    reaped = []
    for row in rows:
        if len(row) < 2:
            continue
        name, activity_text = row[0], row[1]
        try:
            activity = float(activity_text)
        except ValueError:
            continue
        if now - activity > threshold_hours * 3600 and kill(name):
            reaped.append(name)
    return reaped
