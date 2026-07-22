"""跨扫描器共享的纯函数 helper。

各运行时扫描器（scan_claude.py/scan_codex.py/scan_opencode.py/scan_kimi.py/scan_cursor.py）
互不依赖，但各自都需要几个完全相同的小工具函数；集中到这里避免多份重复实现
各自演进出细微差异。这里只放无状态、无副作用的纯函数，运行时私有的解析格式
仍留在各自的 scan_*.py 里。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime


def shorten_cwd(cwd: str) -> str:
    """把工作目录路径里的用户主目录前缀替换为 ~，用于列表页展示。"""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def is_ephemeral_agent_cwd(cwd: str) -> bool:
    """OpenConductor 管家等自动任务写在 /tmp/oc-manager-* 下的临时 cwd。

    这类目录会随任务创建/删除反复出现：曾经因「cwd 不存在」被滤掉的旧会话，
    在目录复活后会整批重新进入扫描结果，被 SessionStore 当成「新会话」插到
    列表最前，造成侧边栏被几天前的管家会话刷屏。扫描阶段直接丢弃。
    """
    if not cwd:
        return False
    normalized = cwd.replace("\\", "/").rstrip("/")
    # /tmp/oc-manager-codex/... 、/tmp/oc-manager-claude/... 、以及嵌套变体
    parts = [p for p in normalized.split("/") if p]
    return any(p.startswith("oc-manager-") for p in parts)


def parse_timestamp(value) -> float | None:
    """解析 ISO8601 时间戳字符串（含尾部 Z）为 epoch 秒；非字符串或格式错误返回 None。"""
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


def live_processes(process_name: str) -> list[tuple[int, str]]:
    """返回全部存活同名进程的 ``(pid, 归一化 cwd)`` 列表。

    与 `live_pids_by_process_name` 不同，这里**不会**按 cwd 去重——同一工作目录
    下可以同时跑多个 agent（例如跨助手接力新建的 Cursor 与旧的 `--resume`
    会话并存）。调用方若只能保守地标「该目录最新一条」，再自行折叠；若能从
    命令行解析出会话 ID，则应逐进程精确绑定。

    已知局限：同名的其它子命令进程（如 `<name> serve`/`<name> run`）会被一并
    计入。任一环节失败都静默降级为空列表，不抛异常。
    """
    found: list[tuple[int, str]] = []
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", process_name], stderr=subprocess.DEVNULL
        ).decode().split()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return found

    for pid_str in pids:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        cwd = None
        if sys.platform.startswith("linux"):
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
        elif sys.platform == "darwin":
            try:
                out = subprocess.check_output(
                    ["lsof", "-a", "-p", pid_str, "-d", "cwd", "-Fn"], stderr=subprocess.DEVNULL
                ).decode(errors="replace")
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                continue
            for line in out.splitlines():
                if line.startswith("n"):
                    cwd = line[1:]
                    break
        else:
            continue
        if cwd:
            found.append((pid, os.path.realpath(cwd)))
    return found


def live_pids_by_process_name(process_name: str) -> dict[str, int]:
    """返回「工作目录 -> pid」映射，供没有 pid 注册表、也不能靠 lsof 定位单个
    历史文件的运行时（OpenCode、Kimi Code）复用同一套判活思路：找到存活的
    同名进程，读取其当前工作目录，与会话记录的工作目录字段匹配。

    同一 cwd 有多个同名进程时只保留其中一个（遍历顺序下的最后一个）。调用方
    需要自行只把该目录最新一条会话标记存活。需要保留全部进程时改用
    `live_processes`。任一环节失败都静默降级为空集，不抛异常。
    """
    live: dict[str, int] = {}
    for pid, cwd in live_processes(process_name):
        live[cwd] = pid
    return live


def process_command_line(pid: int) -> str:
    """读取进程命令行；失败返回空串。供扫描器从 `--resume <id>` 等参数精确绑会话。"""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
        )
        return out.decode(errors="replace").strip()
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return ""


def process_environ(pid: int) -> dict[str, str]:
    """读取进程环境变量；失败返回空字典。

    供扫描器从托管注入的 ``PICKUP_SESSION_ID`` / ``SC_SESSION_ID`` 精确绑会话。
    Linux 读 ``/proc/<pid>/environ``；macOS 用 ``ps eww``（输出混在命令行尾部）。
    """
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
            if not raw:
                return {}
            env: dict[str, str] = {}
            for item in raw.split(b"\x00"):
                if not item or b"=" not in item:
                    continue
                key, value = item.decode(errors="replace").split("=", 1)
                env[key] = value
            return env
        out = subprocess.check_output(
            ["ps", "eww", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return {}
    env: dict[str, str] = {}
    # ps eww 把环境变量拼在同一行；只提取我们关心的键，避免把命令参数误当环境。
    for key in (
        "PICKUP_SESSION_ID",
        "SC_SESSION_ID",
        "PICKUP_RUNTIME",
        "SC_RUNTIME",
    ):
        marker = f"{key}="
        start = out.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = start
        while end < len(out) and not out[end].isspace():
            end += 1
        env[key] = out[start:end]
    return env


def open_file_paths(pids: list[int]) -> dict[int, list[str]]:
    """批量读取进程打开的文件路径；失败的 pid 不出现在结果里。

    Linux 读 ``/proc/<pid>/fd``；其余平台一次 ``lsof -Fn``。
    供 Cursor 等从打开的 ``store.db`` 反推真实会话 ID。
    """
    if not pids:
        return {}
    result: dict[int, list[str]] = {pid: [] for pid in pids}
    if sys.platform.startswith("linux"):
        for pid in pids:
            fd_dir = f"/proc/{pid}/fd"
            try:
                names = os.listdir(fd_dir)
            except OSError:
                result.pop(pid, None)
                continue
            paths: list[str] = []
            for name in names:
                try:
                    paths.append(os.readlink(os.path.join(fd_dir, name)))
                except OSError:
                    continue
            result[pid] = paths
        return {pid: paths for pid, paths in result.items() if paths is not None}

    try:
        out = subprocess.check_output(
            ["lsof", "-Fn", "-p", ",".join(str(pid) for pid in pids)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
            continue
        if current is None or current not in result:
            continue
        if line.startswith("n"):
            result[current].append(line[1:])
    return result
