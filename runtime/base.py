"""运行时适配器抽象。"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod

from models import Handoff, LaunchPlan, SessionInfo


class LaunchError(RuntimeError):
    """启动计划无法安全执行。"""


def usable_cwd(cwd: str | None) -> str | None:
    """只返回当前机器真实存在的工作目录。"""
    return cwd if cwd and os.path.isdir(cwd) else None


class BaseRuntime(ABC):
    """每个命令行 Agent 运行时需要实现的最小能力集合。"""

    id: str
    display_name: str
    executable: str
    history_reading_hint: str

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    @abstractmethod
    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        """扫描并返回该运行时的本地会话。"""

    @abstractmethod
    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        """构造原运行时原生恢复计划。"""

    @abstractmethod
    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        """构造读取其他运行时历史的新会话计划。"""

    def export_handoff(self, session: SessionInfo, title: str) -> Handoff:
        """把运行时私有会话导出为统一接力信息。"""
        raw_history_path = str(session.get("path") or "")
        if not raw_history_path:
            raise LaunchError("原会话未记录历史文件路径")
        history_path = os.path.abspath(raw_history_path)
        if not os.path.isfile(history_path):
            raise LaunchError(f"原会话历史文件不存在：{history_path}")
        return Handoff(
            source_runtime_id=self.id,
            source_runtime_name=self.display_name,
            title=title,
            history_path=history_path,
            original_cwd=str(session.get("cwd") or ""),
            history_reading_hint=self.history_reading_hint,
        )
