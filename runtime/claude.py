"""Claude Code 运行时适配器。"""

from __future__ import annotations

import os

import scan_claude
from models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from runtime.base import BaseRuntime, usable_cwd


class ClaudeRuntime(BaseRuntime):
    id = "claude"
    display_name = "Claude"
    executable = "claude"
    history_reading_hint = (
        "Claude Code JSONL；重点关注 user、assistant、tool_use、tool_result、"
        "last-prompt 等记录及其 message.content。"
    )
    auto_approve_args = ("--dangerously-skip-permissions",)

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_claude.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_claude.load_conversation(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 Claude 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
                "--print",
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                "--add-dir",
                history_dir,
                *self.auto_approve_args,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
            ),
            cwd=usable_cwd(cwd),
        )
