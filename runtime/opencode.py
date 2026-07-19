"""OpenCode CLI 运行时适配器。"""

from __future__ import annotations

import dataclasses

import scan_opencode
from models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from runtime.base import BaseRuntime, usable_cwd


class OpenCodeRuntime(BaseRuntime):
    id = "opencode"
    display_name = "OpenCode"
    executable = "opencode"
    history_reading_hint = (
        "OpenCode 会话历史存在 SQLite 数据库 opencode.db 里（session/message/part 三表，"
        "正文在 part.data 的 JSON text 字段，助手消息元数据在 message.data）；优先运行 "
        "`opencode export <会话ID>` 导出该会话完整 JSON 阅读，或用只读方式"
        "（sqlite3 \"file:<路径>?mode=ro\"）查询，不要写这个库。"
    )
    # 注意：--dangerously-skip-permissions 只在 `opencode run` 子命令下被接受；
    # 主命令（TUI，即 -s/--prompt/裸启动 三种交互路径）的 yargs 校验是严格模式，
    # 带上这个未声明的 flag 会直接报错退出（实测 exit=1），而不是像文档假设的
    # 那样被静默忽略。所以这里不把它放进 auto_approve_args——那会被
    # registry.build_passthrough_plan 无条件拼进 `pickup opencode` 裸启动，反而
    # 直接打不开。只在 build_continue_plan（走 `run` 子命令）里硬编码这一份，
    # 是该参数在 OpenCode 上唯一被证实可用的位置。详见 MAINTAINER_GUIDE。
    auto_approve_args = ()
    _RUN_AUTO_APPROVE_ARG = "--dangerously-skip-permissions"

    def scan_signature(self) -> object | None:
        return scan_opencode.scan_signature()

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_opencode.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_opencode.load_conversation(
            str(session.get("path") or ""), str(session.get("id") or "")
        )

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, "-s", str(session["id"])),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 OpenCode 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                "run",
                self._RUN_AUTO_APPROVE_ARG,
                "-s",
                str(session["id"]),
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        # OpenCode 主命令的位置参数是项目路径，提示词只能通过 --prompt 传入；也没有
        # --add-dir 等价物。主命令不接受跳过权限的 flag（见上方说明），读取源历史时
        # 目标 opencode 若触发权限询问，需要用户在 TUI 里手动确认，这是相对
        # claude/codex 的已知能力差距，已记入 MAINTAINER_GUIDE。
        return LaunchPlan(
            argv=(self.executable, "--prompt", handoff.render_prompt()),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(argv=(self.executable,), cwd=usable_cwd(cwd))

    def export_handoff(self, session: SessionInfo, title: str) -> Handoff:
        """在基类通用实现之上补充会话 ID：历史 db 是全库共享的，没有 ID 无法定位会话。"""
        handoff = super().export_handoff(session, title)
        return dataclasses.replace(
            handoff,
            history_reading_hint=(
                f"{self.history_reading_hint}本次要读取的会话 ID：{session['id']}。"
            ),
        )
