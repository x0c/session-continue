"""运行时注册、接力编排与最终进程替换。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from models import LaunchPlan, LaunchRequest, NewSessionRequest, SessionInfo
from runtime.base import BaseRuntime, LaunchError
from runtime.claude import ClaudeRuntime
from runtime.codex import CodexRuntime


class RuntimeRegistry:
    """按注册顺序管理所有运行时，界面和接力逻辑只依赖本注册表。"""

    def __init__(self, runtimes: Iterable[BaseRuntime]):
        self._runtimes: dict[str, BaseRuntime] = {}
        for runtime in runtimes:
            if runtime.id in self._runtimes:
                raise ValueError(f"运行时重复注册：{runtime.id}")
            self._runtimes[runtime.id] = runtime
        if not self._runtimes:
            raise ValueError("至少需要注册一个运行时")

    def __iter__(self):
        return iter(self._runtimes.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._runtimes)

    def get(self, runtime_id: str) -> BaseRuntime:
        try:
            return self._runtimes[runtime_id]
        except KeyError as exc:
            raise LaunchError(f"未注册的运行时：{runtime_id}") from exc

    def scan_all(self, limit: int) -> dict[str, list[SessionInfo]]:
        """并发扫描各运行时。各适配器只读各自独立的历史目录，互不干扰，
        用线程池重叠磁盘 I/O 等待时间即可，不需要多进程。"""
        runtimes = list(self)
        with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
            scanned = pool.map(lambda runtime: runtime.scan_sessions(limit), runtimes)
        return {runtime.id: result for runtime, result in zip(runtimes, scanned)}

    def build_launch_plan(self, request: LaunchRequest) -> LaunchPlan:
        source_id = str(request.session.get("source") or "")
        source = self.get(source_id)
        target = self.get(request.target_runtime_id)
        if source.id == target.id:
            return source.build_resume_plan(request.session)
        handoff = source.export_handoff(request.session, request.title)
        return target.build_new_plan(handoff)

    def build_new_session_plan(self, request: NewSessionRequest) -> LaunchPlan:
        """构造不关联任何已有会话历史的空白新会话计划。"""
        return self.get(request.target_runtime_id).build_new_session_plan(request.cwd)

    def build_passthrough_plan(self, runtime_id: str, user_args: Iterable[str]) -> LaunchPlan:
        """构造直启透传计划：`sc <runtime> [参数…]`，参数原样交给运行时，只垫上默认全自动放行参数。

        用户已经在 user_args 里显式带了该运行时的放行参数时不重复添加，尊重用户的显式选择。
        """
        runtime = self.get(runtime_id)
        user_args = tuple(user_args)
        extra = tuple(arg for arg in runtime.auto_approve_args if arg not in user_args)
        return LaunchPlan(argv=(runtime.executable, *extra, *user_args), cwd=None)


def default_registry() -> RuntimeRegistry:
    """创建默认运行时注册表；新增运行时只需在这里注册一次。"""
    return RuntimeRegistry((ClaudeRuntime(), CodexRuntime()))


def execute_launch(plan: LaunchPlan) -> None:
    """校验启动计划并让目标运行时接管当前终端。"""
    executable = plan.argv[0]
    if shutil.which(executable) is None:
        raise LaunchError(f"未找到 {executable} 命令，请先安装对应运行时")
    if plan.cwd:
        os.chdir(plan.cwd)
    try:
        os.execvp(executable, list(plan.argv))
    except OSError as exc:
        raise LaunchError(f"无法启动 {executable}：{exc}") from exc
