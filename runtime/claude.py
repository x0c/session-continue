"""Claude Code 运行时适配器。"""

from __future__ import annotations

import os

import scan_claude
from models import Handoff, LaunchPlan, SessionInfo
from runtime.base import BaseRuntime, usable_cwd


class ClaudeRuntime(BaseRuntime):
    id = "claude"
    display_name = "Claude"
    executable = "claude"
    history_reading_hint = (
        "Claude Code JSONL；重点关注 user、assistant、tool_use、tool_result、"
        "last-prompt 等记录及其 message.content。"
    )

    def scan_sessions(self, limit: int) -> list[SessionInfo]:
        return scan_claude.scan_sessions(limit=limit)

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                "--dangerously-skip-permissions",
                "--resume",
                str(session["id"]),
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
                "--dangerously-skip-permissions",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

