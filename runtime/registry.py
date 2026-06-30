"""运行时注册、接力编排与最终进程替换。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable

from models import LaunchPlan, LaunchRequest, SessionInfo
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
        return {runtime.id: runtime.scan_sessions(limit) for runtime in self}

    def build_launch_plan(self, request: LaunchRequest) -> LaunchPlan:
        source_id = str(request.session.get("source") or "")
        source = self.get(source_id)
        target = self.get(request.target_runtime_id)
        if source.id == target.id:
            return source.build_resume_plan(request.session)
        handoff = source.export_handoff(request.session, request.title)
        return target.build_new_plan(handoff)


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
