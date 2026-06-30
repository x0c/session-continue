"""Codex CLI 运行时适配器。"""

from __future__ import annotations

import os

import scan_codex
from models import Handoff, LaunchPlan, SessionInfo
from runtime.base import BaseRuntime, usable_cwd


class CodexRuntime(BaseRuntime):
    id = "codex"
    display_name = "Codex"
    executable = "codex"
    history_reading_hint = (
        "Codex rollout JSONL；重点关注 session_meta、event_msg、response_item，"
        "以及 user_message、agent_message、task_complete、工具调用与结果。"
    )

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_codex.scan_sessions(limit=limit)

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                "resume",
                "-c",
                'model_reasoning_effort="high"',
                "--dangerously-bypass-approvals-and-sandbox",
                str(session["id"]),
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
                "--dangerously-bypass-approvals-and-sandbox",
                "--add-dir",
                history_dir,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

