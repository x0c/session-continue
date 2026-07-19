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


def live_pids_by_process_name(process_name: str) -> dict[str, int]:
    """返回「工作目录 -> pid」映射，供没有 pid 注册表、也不能靠 lsof 定位单个
    历史文件的运行时（OpenCode、Kimi Code）复用同一套判活思路：找到存活的
    同名进程，读取其当前工作目录，与会话记录的工作目录字段匹配。

    已知局限：同名的其它子命令进程（如 `<name> serve`/`<name> run`）会被一并
    计入；同一目录下的多个历史会话，调用方需要自行只把最新一条标记存活。
    任一环节失败都静默降级为空集，不抛异常。
    """
    live: dict[str, int] = {}
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", process_name], stderr=subprocess.DEVNULL
        ).decode().split()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return live

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
            live[os.path.realpath(cwd)] = pid
    return live
