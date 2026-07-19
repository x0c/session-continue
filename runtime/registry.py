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
from runtime.cursor import CursorRuntime
from runtime.kimi import KimiRuntime
from runtime.opencode import OpenCodeRuntime


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
        # 廉价预检缓存：runtime.id -> (limit, scan_signature() 返回值) 与对应的上一次
        # 扫描结果，只有实现了 scan_signature()（非 None）的运行时才会命中，见
        # scan_all() 和 BaseRuntime.scan_signature 的文档。只应由同一调用方顺序调用
        # scan_all()（如 SessionStore 的后台重扫循环），不是线程安全的并发写结构——
        # 调用方需要自己保证同一 registry 实例不会被多个线程同时 scan_all()。
        self._scan_cache: dict[str, tuple[int, object]] = {}
        self._scan_cache_result: dict[str, list[SessionInfo]] = {}

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
        用线程池重叠磁盘 I/O 等待时间即可，不需要多进程。

        单个运行时的扫描异常（如某条真实会话记录格式异常触发未预料的解析
        bug）被隔离在这里：该运行时降级为空列表，不拖垮其余运行时的结果，
        也不让 pickup 首屏因为一条脏数据直接崩溃退出。

        实现了 `scan_signature()`（目前只有 OpenCode）的运行时会先做一次廉价
        签名比对：签名和上一次调用相同就直接复用上一次的扫描结果，跳过完整的
        `scan_sessions()`；没实现（返回 `None`）的运行时不受影响，行为与优化前
        完全一致。见 `BaseRuntime.scan_signature` 文档里为什么 Claude/Codex 故意
        不接入这个机制。
        """
        runtimes = list(self)

        def _copy_sessions(sessions: list[SessionInfo]) -> list[SessionInfo]:
            """缓存与调用方之间隔离可变会话字典，避免界面注入字段反向污染缓存。"""
            return [dict(session) for session in sessions]

        def _scan_one(runtime: BaseRuntime) -> list[SessionInfo]:
            try:
                signature = runtime.scan_signature()
            except Exception:
                signature = None
            if signature is not None:
                cache_key = (limit, signature)
                if self._scan_cache.get(runtime.id) == cache_key:
                    return _copy_sessions(self._scan_cache_result.get(runtime.id, []))
            try:
                result = runtime.scan_sessions(limit)
            except Exception:
                # 瞬时读取失败不能把一份空结果写进新签名、覆盖最后一次成功缓存；
                # 有旧数据时继续展示旧快照，首次扫描就失败才降级为空列表。
                cached = self._scan_cache_result.get(runtime.id)
                return _copy_sessions(cached[:limit]) if cached is not None else []
            if signature is not None:
                self._scan_cache[runtime.id] = (limit, signature)
                # 保存一份、返回另一份：SessionStore/keepalive 会就地给调用方拿到的
                # dict 注入 keepalive_name 等展示状态，不能让这些字段进入扫描缓存。
                self._scan_cache_result[runtime.id] = _copy_sessions(result)
                return _copy_sessions(self._scan_cache_result[runtime.id])
            return result

        with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
            scanned = pool.map(_scan_one, runtimes)
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
    return RuntimeRegistry(
        (ClaudeRuntime(), CodexRuntime(), OpenCodeRuntime(), KimiRuntime(), CursorRuntime())
    )


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
