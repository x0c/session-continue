"""运行时适配器抽象。"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod

from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo


class LaunchError(RuntimeError):
    """启动计划无法安全执行。"""


def usable_cwd(cwd: str | None) -> str | None:
    """只返回当前机器真实存在的工作目录。"""
    return cwd if cwd and os.path.isdir(cwd) else None


_DIGEST_FIRST_LEN = 300  # 摘录里【原始需求】的截断长度
_DIGEST_MSG_LEN = 200  # 摘录里每条最近消息的截断长度
_DIGEST_RECENT_COUNT = 8  # 摘录保留的最近消息条数


def _clip(text: str | None, limit: int) -> str:
    """压平换行成单行并截断；摘录逐行列消息，多行原文会破坏行结构。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


class BaseRuntime(ABC):
    """每个命令行 Agent 运行时需要实现的最小能力集合。"""

    id: str
    display_name: str
    executable: str
    history_reading_hint: str
    auto_approve_args: tuple[str, ...] = ()  # 全自动放行参数（跳过权限审批），供直启子命令复用

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def scan_signature(self) -> object | None:
        """返回一个廉价、可哈希的"本地历史是否可能有变化"签名，供 `RuntimeRegistry.scan_all`
        判断能否跳过一次完整的 `scan_sessions()`（后台重扫最重的开销）。

        只允许做目录/文件级别的元数据探测（`os.stat`/`os.listdir`），不能读文件内容；
        两次调用签名相等即视为"本地历史没有变化"，跳过扫描、复用上一次结果。返回
        `None` 表示该运行时没有可靠的廉价预检，调用方每次都必须完整扫描——这是安全
        默认值。新增运行时或没有用真实数据验证过目录 mtime 语义前，不要覆写为非
        `None`（Claude/Codex 的历史目录是多层嵌套结构，已验证"已有会话文件被追加
        写入"这类变化不会冒泡到任何祖先目录的 mtime，父目录 mtime 判断在这类结构上
        不可靠，因此故意不覆写，保持返回 `None`；OpenCode 是单文件 SQLite，mtime
        判断可靠，见 `runtime/opencode.py`）。"""
        return None

    @abstractmethod
    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        """扫描并返回该运行时的本地会话。"""

    @abstractmethod
    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        """按时间顺序读取用户消息和每轮最终答复。"""

    @abstractmethod
    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        """构造原运行时原生恢复计划。"""

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造携带新指令的非交互式原生续接计划，但不执行。

        保留默认实现，避免既有第三方适配器因新增可选能力无法实例化；调用方会把
        此异常转为结构化的“不支持续接计划”结果。
        """
        raise LaunchError(f"运行时 {self.id} 尚未支持携带新指令的续接计划")

    @abstractmethod
    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        """构造读取其他运行时历史的新会话计划。"""

    @abstractmethod
    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        """构造不关联任何已有会话历史的空白新会话计划。"""

    def delete_session(self, session: SessionInfo) -> None:
        """彻底删除该会话在本地磁盘上的历史，不可恢复。

        保留默认实现（而非 abstractmethod），原因同 `build_continue_plan`：避免
        既有第三方适配器因新增可选能力无法实例化。默认直接报错，调用方（TUI）
        据此提示"该运行时尚未支持删除"。
        """
        raise LaunchError(f"运行时 {self.id} 尚未支持删除会话")

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
            conversation_digest=self._conversation_digest(session),
        )

    def _conversation_digest(self, session: SessionInfo) -> str:
        """构建接力提示词里的对话摘录；任何失败都降级为空串，不阻断接力。

        标题最长十几个字，作为任务说明极度有损；原始 JSONL 尾部又常是工具结果、
        系统注入事件等噪音，冷启动的目标 agent 首次解析容易定位错重点。这里用
        预览同款 load_conversation（已过滤系统事件、None 兜底）提取一段干净摘录
        做锚点。角色标"用户"而不是"你"——摘录是给接手的大模型看的，"你"会被
        误解为指它自己。
        """
        try:
            messages = self.load_conversation(session)
        except Exception:
            messages = []

        lines: list[str] = []
        if messages:
            recent = messages[-_DIGEST_RECENT_COUNT:]
            first_user = next((m for m in messages if m.role == "user"), None)
            if first_user is not None and first_user not in recent:
                lines.append("【原始需求】" + _clip(first_user.text, _DIGEST_FIRST_LEN))
            lines.append("【最近对话】")
            for message in recent:
                role = "用户" if message.role == "user" else "助手"
                lines.append(f"{role}: {_clip(message.text, _DIGEST_MSG_LEN)}")
            return "\n".join(lines)

        # 对话提取失败/为空时，回退扫描层已截好的首尾消息，尽量给出锚点。
        first = _clip(session.get("first_user_msg"), _DIGEST_FIRST_LEN)
        last_user = _clip(session.get("last_user_msg"), _DIGEST_MSG_LEN)
        last_agent = _clip(session.get("last_agent_msg"), _DIGEST_MSG_LEN)
        if first:
            lines.append("【原始需求】" + first)
        recent_lines = []
        if last_user and last_user != first:
            recent_lines.append(f"用户: {last_user}")
        if last_agent:
            recent_lines.append(f"助手: {last_agent}")
        if recent_lines:
            lines.append("【最近对话】")
            lines.extend(recent_lines)
        return "\n".join(lines)
