"""Codex CLI 运行时适配器。"""

from __future__ import annotations

import os

from pickup.scan import codex as scan_codex
from pickup.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from pickup.runtime.base import BaseRuntime, usable_cwd


class CodexRuntime(BaseRuntime):
    id = "codex"
    display_name = "Codex"
    executable = "codex"
    history_reading_hint = (
        "Codex rollout JSONL；重点关注 session_meta、event_msg、response_item，"
        "以及 user_message、agent_message、task_complete、工具调用与结果。"
    )
    auto_approve_args = ("--dangerously-bypass-approvals-and-sandbox",)

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_codex.scan_sessions(limit=limit)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        return scan_codex.load_conversation(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                "resume",
                "-c",
                'model_reasoning_effort="high"',
                *self.auto_approve_args,
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 Codex 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                "exec",
                "resume",
                "-c",
                'model_reasoning_effort="high"',
                *self.auto_approve_args,
                str(session["id"]),
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                "-c",
                'model_reasoning_effort="high"',
                *self.auto_approve_args,
                "--add-dir",
                history_dir,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                "-c",
                'model_reasoning_effort="high"',
                *self.auto_approve_args,
            ),
            cwd=usable_cwd(cwd),
        )
